#import <Foundation/Foundation.h>
#import <Cocoa/Cocoa.h>
#import <objc/runtime.h>
#import <objc/message.h>

#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <unistd.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <sys/stat.h>

#include <thread>
#include <atomic>
#include <string>
#include <sstream>
#include <fstream>
#include <vector>
#include <chrono>
#include <mutex>
#include <cstdlib>

// ObjectARX / libaccore / libacdb function pointer types
struct resbuf {
    struct resbuf* rbnext;
    short restype;
    short _pad[3];
    union {
        double rreal;
        double rpoint[3];
        short rint;
        wchar_t* rstring;
        int32_t rlong;
        int64_t mnLongPtr;
    } resval;
};

using FnRegisterAppMDIAware = int (*)(void*);
using FnPostCommand = int (*)(const wchar_t*);
using FnEvaluateLisp = int (*)(const wchar_t*, resbuf*&);
using FnAcutRelRb = int (*)(resbuf*);
using FnAcutPrintf = int (*)(const wchar_t*, ...);
using FnGetVar = int (*)(const wchar_t*, resbuf*);
using FnCommandActive = int (*)();
using FnInQuiescentState = bool (*)();

static FnRegisterAppMDIAware g_fnRegisterAppMDIAware = nullptr;
static FnPostCommand g_fnPostCommand = nullptr;
static FnEvaluateLisp g_fnEvaluateLisp = nullptr;
static FnAcutRelRb g_fnAcutRelRb = nullptr;
static FnAcutPrintf g_fnAcutPrintf = nullptr;
static FnGetVar g_fnGetVar = nullptr;
static FnCommandActive g_fnCommandActive = nullptr;
static FnInQuiescentState g_fnInQuiescentState = nullptr;

static std::atomic<bool> g_serverRunning{false};
static std::atomic<int> g_serverSock{-1};
static std::atomic<uint64_t> g_reqCounter{0};
static std::mutex g_execMutex;

static std::string detectHelpersLspPath() {
    const char* helpersEnv = getenv("AUTOCAD_MCP_HELPERS_LSP");
    if (helpersEnv && strlen(helpersEnv) > 0) {
        return std::string(helpersEnv);
    }
    Dl_info info;
    if (dladdr(reinterpret_cast<const void*>(&detectHelpersLspPath), &info) && info.dli_fname) {
        NSString* fullPath = [NSString stringWithUTF8String:info.dli_fname];
        NSString* dir = [fullPath stringByDeletingLastPathComponent];
        for (int i = 0; i < 4; ++i) {
            NSString* candidate = [dir stringByAppendingPathComponent:@"mcp_helpers.lsp"];
            if ([[NSFileManager defaultManager] fileExistsAtPath:candidate]) {
                return std::string([candidate UTF8String]);
            }
            dir = [dir stringByDeletingLastPathComponent];
        }
    }
    return "/Users/charlie/.gemini/antigravity/scratch/autocad-mcp/bridge/mcp_helpers.lsp";
}

static void resolveArxSymbols() {
    if (!g_fnRegisterAppMDIAware) {
        g_fnRegisterAppMDIAware = reinterpret_cast<FnRegisterAppMDIAware>(
            dlsym(RTLD_DEFAULT, "_Z23acrxRegisterAppMDIAwarePv"));
    }
    if (!g_fnPostCommand) {
        g_fnPostCommand = reinterpret_cast<FnPostCommand>(
            dlsym(RTLD_DEFAULT, "_Z15acedPostCommandPKw"));
        if (!g_fnPostCommand) {
            g_fnPostCommand = reinterpret_cast<FnPostCommand>(
                dlsym(RTLD_DEFAULT, "_Z17postCommandStringPKwb"));
        }
    }
    if (!g_fnEvaluateLisp) {
        g_fnEvaluateLisp = reinterpret_cast<FnEvaluateLisp>(
            dlsym(RTLD_DEFAULT, "_Z16acedEvaluateLispPKwRP6resbuf"));
    }
    if (!g_fnAcutRelRb) {
        g_fnAcutRelRb = reinterpret_cast<FnAcutRelRb>(
            dlsym(RTLD_DEFAULT, "_Z9acutRelRbP6resbuf"));
    }
    if (!g_fnAcutPrintf) {
        g_fnAcutPrintf = reinterpret_cast<FnAcutPrintf>(
            dlsym(RTLD_DEFAULT, "_Z10acutPrintfPKwz"));
    }
    if (!g_fnGetVar) {
        g_fnGetVar = reinterpret_cast<FnGetVar>(
            dlsym(RTLD_DEFAULT, "_Z10acedGetVarPKwP6resbuf"));
    }
    if (!g_fnCommandActive) {
        g_fnCommandActive = reinterpret_cast<FnCommandActive>(
            dlsym(RTLD_DEFAULT, "_Z17acedCommandActivev"));
    }
    if (!g_fnInQuiescentState) {
        g_fnInQuiescentState = reinterpret_cast<FnInQuiescentState>(
            dlsym(RTLD_DEFAULT, "_Z20acadInQuiescentStatev"));
    }
}

static std::wstring utf8ToWstring(const std::string& utf8) {
    if (utf8.empty()) return std::wstring();
    NSString* ns = [[NSString alloc] initWithBytes:utf8.data()
                                            length:utf8.size()
                                          encoding:NSUTF8StringEncoding];
    if (!ns) return std::wstring();
    NSUInteger bytes = [ns lengthOfBytesUsingEncoding:NSUTF32LittleEndianStringEncoding];
    if (bytes == 0) return std::wstring();
    std::wstring wstr(bytes / sizeof(wchar_t), L'\0');
    NSUInteger used = 0;
    [ns getBytes:&wstr[0]
       maxLength:bytes
      usedLength:&used
        encoding:NSUTF32LittleEndianStringEncoding
         options:0
           range:NSMakeRange(0, [ns length])
  remainingRange:nullptr];
    return wstr;
}

static std::string wstringToUtf8(const wchar_t* wstr) {
    if (!wstr) return "";
    size_t len = wcslen(wstr);
    if (len == 0) return "";
    NSString* ns = [[NSString alloc] initWithBytes:wstr
                                            length:len * sizeof(wchar_t)
                                          encoding:NSUTF32LittleEndianStringEncoding];
    if (!ns) return "";
    const char* cstr = [ns UTF8String];
    return cstr ? std::string(cstr) : "";
}

static std::string escapeJsonString(const std::string& input) {
    std::ostringstream ss;
    for (unsigned char c : input) {
        switch (c) {
            case '"': ss << "\\\""; break;
            case '\\': ss << "\\\\"; break;
            case '\b': ss << "\\b"; break;
            case '\f': ss << "\\f"; break;
            case '\n': ss << "\\n"; break;
            case '\r': ss << "\\r"; break;
            case '\t': ss << "\\t"; break;
            default:
                if (c < 0x20) {
                    char buf[8];
                    snprintf(buf, sizeof(buf), "\\u%04x", c);
                    ss << buf;
                } else {
                    ss << c;
                }
        }
    }
    return ss.str();
}

static void wakeCocoaEventLoop() {
    @autoreleasepool {
        if (!NSApp) return;
        NSEvent* ev = [NSEvent otherEventWithType:NSEventTypeApplicationDefined
                                         location:NSZeroPoint
                                    modifierFlags:0
                                        timestamp:[[NSProcessInfo processInfo] systemUptime]
                                     windowNumber:0
                                          context:nil
                                          subtype:0
                                            data1:0
                                            data2:0];
        if (ev) {
            [NSApp postEvent:ev atStart:YES];
        }
    }
}

static bool runOnMainThreadWithTimeout(double timeoutSec, void (^block)()) {
    if ([NSThread isMainThread]) {
        block();
        return true;
    }
    dispatch_semaphore_t sem = dispatch_semaphore_create(0);
    auto completed = std::make_shared<std::atomic<bool>>(false);
    wakeCocoaEventLoop();
    dispatch_async(dispatch_get_main_queue(), ^{
        @autoreleasepool {
            block();
            completed->store(true);
            dispatch_semaphore_signal(sem);
        }
    });
    dispatch_time_t waitTime = dispatch_time(
        DISPATCH_TIME_NOW,
        static_cast<int64_t>(timeoutSec * NSEC_PER_SEC)
    );
    long waited = dispatch_semaphore_wait(sem, waitTime);
    return (waited == 0 && completed->load());
}

static std::string getSysVarStringOnMainThread(const wchar_t* name) {
    if (!g_fnGetVar) return "";
    resbuf rb;
    memset(&rb, 0, sizeof(rb));
    int rc = g_fnGetVar(name, &rb);
    // RTNORM == 5100, RTSTR == 5005
    if (rc == 5100 && rb.restype == 5005 && rb.resval.rstring) {
        std::string val = wstringToUtf8(rb.resval.rstring);
        free(rb.resval.rstring);
        return val;
    }
    return "";
}

static int getSysVarIntOnMainThread(const wchar_t* name, int defaultVal = 0) {
    if (!g_fnGetVar) return defaultVal;
    resbuf rb;
    memset(&rb, 0, sizeof(rb));
    int rc = g_fnGetVar(name, &rb);
    // RTNORM == 5100, RTSHORT == 5003, RTLONG == 5010
    if (rc == 5100) {
        if (rb.restype == 5003) return static_cast<int>(rb.resval.rint);
        if (rb.restype == 5010) return static_cast<int>(rb.resval.rlong);
    }
    return defaultVal;
}

struct MainThreadDiagnostics {
    bool main_thread_responsive = false;
    bool has_drawing_doc = false;
    bool modal_dialog = false;
    std::string modal_window_title;
    bool busy = false;
    bool quiescent = true;
    int cmd_active = 0;
    std::string command_names;
    std::string active_document;
    int document_count = 0;
};

static MainThreadDiagnostics inspectStatusOnMainThread(bool createDocIfMissing = false, double timeoutSec = 2.0) {
    resolveArxSymbols();
    __block MainThreadDiagnostics diag;

    bool ok = runOnMainThreadWithTimeout(timeoutSec, ^{
        diag.main_thread_responsive = true;
        if (NSApp) {
            NSWindow* modalWin = [NSApp modalWindow];
            if (modalWin != nil) {
                diag.modal_dialog = true;
                NSString* title = [modalWin title];
                if (title) {
                    diag.modal_window_title = std::string([title UTF8String]);
                }
            }
        }

        NSDocumentController* dc = [NSDocumentController sharedDocumentController];
        NSString* fallbackDocPath = nil;
        if (dc) {
            id curDoc = [dc currentDocument];
            if (curDoc) {
                NSURL* curUrl = [curDoc respondsToSelector:@selector(fileURL)] ? [curDoc fileURL] : nil;
                if (curUrl && [curUrl path]) {
                    fallbackDocPath = [curUrl path];
                }
            }
            NSArray* docs = [dc documents];
            for (id doc in docs) {
                BOOL isStart = NO;
                SEL isStartSel = NSSelectorFromString(@"isStartTabDoc");
                if ([doc respondsToSelector:isStartSel]) {
                    isStart = ((BOOL (*)(id, SEL))objc_msgSend)(doc, isStartSel);
                }
                if (!isStart) {
                    diag.has_drawing_doc = true;
                    diag.document_count += 1;
                    if (!fallbackDocPath) {
                        NSURL* fileUrl = [doc respondsToSelector:@selector(fileURL)] ? [doc fileURL] : nil;
                        if (fileUrl && [fileUrl path]) {
                            fallbackDocPath = [fileUrl path];
                        } else if ([doc respondsToSelector:@selector(displayName)]) {
                            fallbackDocPath = [doc displayName];
                        }
                    }
                }
            }
            if (!diag.has_drawing_doc && createDocIfMissing && !diag.modal_dialog) {
                NSError* err = nil;
                id newDoc = [dc openUntitledDocumentAndDisplay:YES error:&err];
                if (newDoc != nil) {
                    diag.has_drawing_doc = true;
                    diag.document_count = 1;
                }
            }
        }

        if (diag.has_drawing_doc) {
            std::string dwgPrefix = getSysVarStringOnMainThread(L"DWGPREFIX");
            std::string dwgName = getSysVarStringOnMainThread(L"DWGNAME");
            if (!dwgName.empty()) {
                diag.active_document = dwgPrefix + dwgName;
            } else if (fallbackDocPath) {
                diag.active_document = std::string([fallbackDocPath UTF8String]);
            }
            diag.command_names = getSysVarStringOnMainThread(L"CMDNAMES");
            diag.cmd_active = getSysVarIntOnMainThread(L"CMDACTIVE", 0);
            if (g_fnCommandActive) {
                int ca = g_fnCommandActive();
                if (ca != 0 && diag.cmd_active == 0) {
                    diag.cmd_active = ca;
                }
            }
            if (g_fnInQuiescentState) {
                diag.quiescent = g_fnInQuiescentState();
            } else {
                diag.quiescent = ((diag.cmd_active & 11) == 0 && diag.command_names.empty());
            }
        }

        // Only interactive commands (bits 1, 2, 8 of CMDACTIVE) or non-empty CMDNAMES count as busy
        diag.busy = diag.modal_dialog || ((diag.cmd_active & 11) != 0) || (!diag.command_names.empty());
    });

    if (!ok) {
        diag.main_thread_responsive = false;
        diag.busy = true;
    }
    return diag;
}

static std::string buildStatusJson(const MainThreadDiagnostics& diag) {
    bool drawingReady = diag.main_thread_responsive && diag.has_drawing_doc && !diag.modal_dialog;
    bool cmdExecutable = drawingReady && !diag.busy;
    std::ostringstream ss;
    ss << "{"
       << "\"ok\":true,"
       << "\"app\":\"AutoCAD 2027\","
       << "\"bridge_version\":\"0.2.0\","
       << "\"bridge_online\":true,"
       << "\"main_thread_responsive\":" << (diag.main_thread_responsive ? "true" : "false") << ","
       << "\"drawing_ready\":" << (drawingReady ? "true" : "false") << ","
       << "\"command_executable\":" << (cmdExecutable ? "true" : "false") << ","
       << "\"has_drawing_document\":" << (diag.has_drawing_doc ? "true" : "false") << ","
       << "\"document_count\":" << diag.document_count << ","
       << "\"active_document\":\"" << escapeJsonString(diag.active_document) << "\","
       << "\"command_names\":\"" << escapeJsonString(diag.command_names) << "\","
       << "\"cmd_active\":" << diag.cmd_active << ","
       << "\"quiescent\":" << (diag.quiescent ? "true" : "false") << ","
       << "\"busy\":" << (diag.busy ? "true" : "false") << ","
       << "\"modal_dialog\":" << (diag.modal_dialog ? "true" : "false") << ","
       << "\"modal_window_title\":\"" << escapeJsonString(diag.modal_window_title) << "\""
       << "}";
    return ss.str();
}

static std::string executeLispViaCommandQueue(
    const std::string& lispCode,
    int timeoutMs = 15000,
    bool autoCancelActiveCommand = false
) {
    std::lock_guard<std::mutex> lock(g_execMutex);
    resolveArxSymbols();
    auto t0 = std::chrono::steady_clock::now();

    uint64_t seq = ++g_reqCounter;
    std::string reqIdStr = "live-" + std::to_string(seq);

    MainThreadDiagnostics preDiag = inspectStatusOnMainThread(true, 2.5);
    // If AutoCAD is briefly finishing the tail of a previous command, wait up to 600ms
    for (int waitIter = 0;
         waitIter < 40 && preDiag.main_thread_responsive && !preDiag.modal_dialog && preDiag.busy;
         ++waitIter) {
        wakeCocoaEventLoop();
        std::this_thread::sleep_for(std::chrono::milliseconds(15));
        preDiag = inspectStatusOnMainThread(false, 1.0);
    }

    auto elapsedPre = std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now() - t0).count();

    if (!preDiag.main_thread_responsive) {
        std::ostringstream err;
        err << "{"
            << "\"ok\":false,"
            << "\"request_id\":\"" << reqIdStr << "\","
            << "\"elapsed_ms\":" << elapsedPre << ","
            << "\"execution_stage\":\"precheck_failed\","
            << "\"error_code\":\"MAIN_THREAD_UNRESPONSIVE\","
            << "\"script_success\":false,"
            << "\"command_success\":false,"
            << "\"busy\":true,"
            << "\"modal_dialog\":" << (preDiag.modal_dialog ? "true" : "false") << ","
            << "\"active_document\":\"" << escapeJsonString(preDiag.active_document) << "\","
            << "\"command_names\":\"" << escapeJsonString(preDiag.command_names) << "\","
            << "\"error\":\"AutoCAD main UI thread did not respond within 2.5s (possible modal dialog or heavy operation in progress)\""
            << "}";
        return err.str();
    }

    if (preDiag.modal_dialog) {
        std::ostringstream err;
        err << "{"
            << "\"ok\":false,"
            << "\"request_id\":\"" << reqIdStr << "\","
            << "\"elapsed_ms\":" << elapsedPre << ","
            << "\"execution_stage\":\"precheck_failed\","
            << "\"error_code\":\"MODAL_DIALOG_ACTIVE\","
            << "\"script_success\":false,"
            << "\"command_success\":false,"
            << "\"busy\":true,"
            << "\"modal_dialog\":true,"
            << "\"modal_window_title\":\"" << escapeJsonString(preDiag.modal_window_title) << "\","
            << "\"active_document\":\"" << escapeJsonString(preDiag.active_document) << "\","
            << "\"command_names\":\"" << escapeJsonString(preDiag.command_names) << "\","
            << "\"error\":\"AutoCAD currently has a modal dialog open ("
            << escapeJsonString(preDiag.modal_window_title.empty() ? "untitled modal" : preDiag.modal_window_title)
            << "); dismiss it before executing live commands\""
            << "}";
        return err.str();
    }

    if (preDiag.busy && !autoCancelActiveCommand) {
        std::ostringstream err;
        err << "{"
            << "\"ok\":false,"
            << "\"request_id\":\"" << reqIdStr << "\","
            << "\"elapsed_ms\":" << elapsedPre << ","
            << "\"execution_stage\":\"precheck_failed\","
            << "\"error_code\":\"COMMAND_ACTIVE_BUSY\","
            << "\"script_success\":false,"
            << "\"command_success\":false,"
            << "\"busy\":true,"
            << "\"modal_dialog\":false,"
            << "\"active_document\":\"" << escapeJsonString(preDiag.active_document) << "\","
            << "\"command_names\":\"" << escapeJsonString(preDiag.command_names) << "\","
            << "\"error\":\"AutoCAD is busy with an active command ("
            << escapeJsonString(preDiag.command_names.empty() ? "CMDACTIVE!=0" : preDiag.command_names)
            << "); wait or cancel the active command before sending new live LISP\""
            << "}";
        return err.str();
    }

    std::string reqPath = "/tmp/autocad_mcp_req_" + std::to_string(seq) + ".lsp";
    std::string respPath = "/tmp/autocad_mcp_resp_" + std::to_string(seq) + ".json";
    std::string respTmpPath = respPath + ".tmp";

    unlink(reqPath.c_str());
    unlink(respPath.c_str());
    unlink(respTmpPath.c_str());

    {
        std::ofstream ofs(reqPath, std::ios::binary);
        ofs << lispCode;
        ofs.close();
    }

    std::string helpersPath = detectHelpersLspPath();

    std::ostringstream cmdBuilder;
    cmdBuilder << "(progn "
               << "(if (not mcp:open-utf8) (load \"" << helpersPath << "\")) "
               << "(mcp:exec-file \"" << reqPath << "\" \"" << respPath << "\")"
               << ")\n";
    std::string cmdUtf8 = cmdBuilder.str();
    std::wstring cmdW = utf8ToWstring(cmdUtf8);
    bool needCancelFirst = (preDiag.busy && autoCancelActiveCommand);

    __block bool posted = false;
    bool postDispatched = runOnMainThreadWithTimeout(2.5, ^{
        if (g_fnPostCommand) {
            if (needCancelFirst) {
                g_fnPostCommand(L"CANCELCMD");
            }
            int rc = g_fnPostCommand(cmdW.c_str());
            posted = (rc != 0);
        } else if (g_fnEvaluateLisp) {
            resbuf* rb = nullptr;
            int rc = g_fnEvaluateLisp(cmdW.c_str(), rb);
            if (rc != 0) {
                posted = true;
            }
            if (rb && g_fnAcutRelRb) {
                g_fnAcutRelRb(rb);
            }
        }
        wakeCocoaEventLoop();
    });

    if (!postDispatched || !posted) {
        unlink(reqPath.c_str());
        auto elapsedFail = std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::steady_clock::now() - t0).count();
        std::ostringstream err;
        err << "{"
            << "\"ok\":false,"
            << "\"request_id\":\"" << reqIdStr << "\","
            << "\"elapsed_ms\":" << elapsedFail << ","
            << "\"execution_stage\":\"dispatch_failed\","
            << "\"error_code\":\"POST_COMMAND_FAILED\","
            << "\"script_success\":false,"
            << "\"command_success\":false,"
            << "\"busy\":" << (preDiag.busy ? "true" : "false") << ","
            << "\"modal_dialog\":" << (preDiag.modal_dialog ? "true" : "false") << ","
            << "\"active_document\":\"" << escapeJsonString(preDiag.active_document) << "\","
            << "\"command_names\":\"" << escapeJsonString(preDiag.command_names) << "\","
            << "\"error\":\"Failed to post command to AutoCAD main thread\""
            << "}";
        return err.str();
    }

    // Wait for respPath to appear, periodically nudging the Cocoa event loop
    // so background AutoCAD processes the queued command immediately.
    int pollCount = 0;
    while (true) {
        struct stat st;
        if (stat(respPath.c_str(), &st) == 0 && st.st_size > 0) {
            std::ifstream ifs(respPath, std::ios::binary);
            std::ostringstream buf;
            buf << ifs.rdbuf();
            ifs.close();
            std::string content = buf.str();
            while (!content.empty() && (content.back() == '\n' || content.back() == '\r' || content.back() == ' ')) {
                content.pop_back();
            }
            if (!content.empty()) {
                unlink(reqPath.c_str());
                unlink(respPath.c_str());
                auto elapsedDone = std::chrono::duration_cast<std::chrono::milliseconds>(
                    std::chrono::steady_clock::now() - t0).count();
                if (content.front() == '{') {
                    std::ostringstream injected;
                    injected << "{"
                             << "\"request_id\":\"" << reqIdStr << "\","
                             << "\"elapsed_ms\":" << elapsedDone << ","
                             << "\"modal_dialog\":false,"
                             << content.substr(1);
                    return injected.str();
                }
                return content;
            }
        }
        auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::steady_clock::now() - t0).count();
        if (elapsed >= timeoutMs) {
            break;
        }
        if (++pollCount % 10 == 0) {
            wakeCocoaEventLoop();
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }

    unlink(reqPath.c_str());
    unlink(respPath.c_str());
    unlink(respTmpPath.c_str());

    MainThreadDiagnostics postDiag = inspectStatusOnMainThread(false, 1.0);
    auto elapsedTimeout = std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now() - t0).count();
    std::ostringstream err;
    err << "{"
        << "\"ok\":false,"
        << "\"request_id\":\"" << reqIdStr << "\","
        << "\"elapsed_ms\":" << elapsedTimeout << ","
        << "\"execution_stage\":\"response_timeout\","
        << "\"error_code\":\"LISP_EXECUTION_TIMEOUT\","
        << "\"script_success\":false,"
        << "\"command_success\":false,"
        << "\"busy\":" << (postDiag.busy ? "true" : "false") << ","
        << "\"modal_dialog\":" << (postDiag.modal_dialog ? "true" : "false") << ","
        << "\"active_document\":\"" << escapeJsonString(postDiag.active_document) << "\","
        << "\"command_names\":\"" << escapeJsonString(postDiag.command_names) << "\","
        << "\"error\":\"Timed out waiting for AutoCAD LISP execution response\""
        << "}";
    return err.str();
}

static std::string postRawCommand(const std::string& rawCmd) {
    resolveArxSymbols();
    MainThreadDiagnostics preDiag = inspectStatusOnMainThread(true, 2.0);
    if (!preDiag.main_thread_responsive) {
        return "{\"ok\":false,\"error_code\":\"MAIN_THREAD_UNRESPONSIVE\",\"busy\":true,\"error\":\"AutoCAD main thread is unresponsive\"}";
    }
    if (preDiag.modal_dialog) {
        return "{\"ok\":false,\"error_code\":\"MODAL_DIALOG_ACTIVE\",\"modal_dialog\":true,\"busy\":true,\"error\":\"AutoCAD has an active modal dialog\"}";
    }

    std::wstring cmdW;
    if (rawCmd == "CANCELCMD" || rawCmd == "\x03" || rawCmd == "\x03\x03" || rawCmd == "^C^C") {
        cmdW = L"CANCELCMD";
    } else {
        std::string cmdWithNewline = rawCmd;
        if (cmdWithNewline.empty() || cmdWithNewline.back() != '\n') {
            cmdWithNewline.push_back('\n');
        }
        cmdW = utf8ToWstring(cmdWithNewline);
    }

    __block bool ok = false;
    bool dispatched = runOnMainThreadWithTimeout(2.0, ^{
        if (g_fnPostCommand) {
            ok = (g_fnPostCommand(cmdW.c_str()) != 0);
        }
        wakeCocoaEventLoop();
    });

    if (dispatched && ok) {
        return "{\"ok\":true,\"status\":\"Command posted to AutoCAD command line\"}";
    }
    return "{\"ok\":false,\"error_code\":\"POST_COMMAND_FAILED\",\"error\":\"Failed to post raw command to AutoCAD\"}";
}

static void handleClientConnection(int clientSock) {
    @autoreleasepool {
        std::string buffer;
        char chunk[8192];
        while (true) {
            ssize_t n = recv(clientSock, chunk, sizeof(chunk), 0);
            if (n <= 0) break;
            buffer.append(chunk, static_cast<size_t>(n));

            size_t newlinePos = std::string::npos;
            while ((newlinePos = buffer.find('\n')) != std::string::npos) {
                std::string line = buffer.substr(0, newlinePos);
                buffer.erase(0, newlinePos + 1);
                if (line.empty()) continue;

                NSData* reqData = [NSData dataWithBytes:line.data() length:line.size()];
                NSError* jsonErr = nil;
                NSDictionary* req = [NSJSONSerialization JSONObjectWithData:reqData options:0 error:&jsonErr];
                if (!req || ![req isKindOfClass:[NSDictionary class]]) {
                    std::string errResp = "{\"jsonrpc\":\"2.0\",\"error\":{\"code\":-32700,\"message\":\"Parse error\"},\"id\":null}\n";
                    send(clientSock, errResp.data(), errResp.size(), 0);
                    continue;
                }

                id reqId = req[@"id"] ?: [NSNull null];
                NSString* method = req[@"method"] ?: @"";
                NSDictionary* params = [req[@"params"] isKindOfClass:[NSDictionary class]] ? req[@"params"] : @{};

                NSData* idData = [NSJSONSerialization dataWithJSONObject:@{@"id": reqId} options:0 error:nil];
                NSString* idWrapper = [[NSString alloc] initWithData:idData encoding:NSUTF8StringEncoding];
                std::string idJson = "null";
                if (idWrapper) {
                    std::string w = [idWrapper UTF8String];
                    size_t colon = w.find(':');
                    size_t endBrace = w.rfind('}');
                    if (colon != std::string::npos && endBrace != std::string::npos && endBrace > colon + 1) {
                        idJson = w.substr(colon + 1, endBrace - colon - 1);
                    }
                }

                if ([method isEqualToString:@"ping"] || [method isEqualToString:@"status"]) {
                    MainThreadDiagnostics diag = inspectStatusOnMainThread(false, 2.0);
                    std::string statusJson = buildStatusJson(diag);
                    std::string resp = "{\"jsonrpc\":\"2.0\",\"result\":" + statusJson + ",\"id\":" + idJson + "}\n";
                    send(clientSock, resp.data(), resp.size(), 0);
                    continue;
                }

                if ([method isEqualToString:@"eval_lisp"]) {
                    NSString* code = params[@"code"] ?: @"";
                    int timeoutMs = params[@"timeout_ms"] ? [params[@"timeout_ms"] intValue] : 15000;
                    bool autoCancel = params[@"auto_cancel"] ? [params[@"auto_cancel"] boolValue] : false;
                    std::string resPayload = executeLispViaCommandQueue([code UTF8String], timeoutMs, autoCancel);
                    std::string resp = "{\"jsonrpc\":\"2.0\",\"result\":" + resPayload + ",\"id\":" + idJson + "}\n";
                    send(clientSock, resp.data(), resp.size(), 0);
                    continue;
                }

                if ([method isEqualToString:@"post_command"]) {
                    NSString* cmd = params[@"command"] ?: @"";
                    std::string resPayload = postRawCommand([cmd UTF8String]);
                    std::string resp = "{\"jsonrpc\":\"2.0\",\"result\":" + resPayload + ",\"id\":" + idJson + "}\n";
                    send(clientSock, resp.data(), resp.size(), 0);
                    continue;
                }

                std::string unknown = "{\"jsonrpc\":\"2.0\",\"error\":{\"code\":-32601,\"message\":\"Method not found\"},\"id\":" + idJson + "}\n";
                send(clientSock, unknown.data(), unknown.size(), 0);
            }
        }
        close(clientSock);
    }
}

static void startSocketServer() {
    @autoreleasepool {
        NSString* procName = [[NSProcessInfo processInfo] processName];
        const char* allowConsole = getenv("AUTOCAD_MCP_ALLOW_CONSOLE_SOCKET");
        if ([procName isEqualToString:@"AcCoreConsole"] &&
            (!allowConsole || strcmp(allowConsole, "1") != 0)) {
            return;
        }
    }
    bool expected = false;
    if (!g_serverRunning.compare_exchange_strong(expected, true)) {
        return;
    }

    std::thread([]() {
        int port = 9877;
        const char* portEnv = getenv("AUTOCAD_MCP_PORT");
        if (portEnv && atoi(portEnv) > 0) {
            port = atoi(portEnv);
        }

        int serverFd = socket(AF_INET, SOCK_STREAM, 0);
        if (serverFd < 0) {
            g_serverRunning = false;
            return;
        }

        int opt = 1;
        setsockopt(serverFd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

        struct sockaddr_in addr;
        memset(&addr, 0, sizeof(addr));
        addr.sin_family = AF_INET;
        addr.sin_addr.s_addr = inet_addr("127.0.0.1");
        addr.sin_port = htons(static_cast<uint16_t>(port));

        if (bind(serverFd, reinterpret_cast<struct sockaddr*>(&addr), sizeof(addr)) < 0) {
            close(serverFd);
            g_serverRunning = false;
            return;
        }

        if (listen(serverFd, 16) < 0) {
            close(serverFd);
            g_serverRunning = false;
            return;
        }

        g_serverSock = serverFd;

        while (g_serverRunning) {
            struct sockaddr_in clientAddr;
            socklen_t clientLen = sizeof(clientAddr);
            int clientFd = accept(serverFd, reinterpret_cast<struct sockaddr*>(&clientAddr), &clientLen);
            if (clientFd < 0) {
                if (!g_serverRunning) break;
                continue;
            }
            std::thread(handleClientConnection, clientFd).detach();
        }

        if (g_serverSock >= 0) {
            close(g_serverSock);
            g_serverSock = -1;
        }
    }).detach();
}

static void stopSocketServer() {
    g_serverRunning = false;
    int fd = g_serverSock.exchange(-1);
    if (fd >= 0) {
        shutdown(fd, SHUT_RDWR);
        close(fd);
    }
}

__attribute__((constructor))
static void onBundleLoaded() {
    resolveArxSymbols();
    startSocketServer();
}

__attribute__((destructor))
static void onBundleUnloaded() {
    stopSocketServer();
}

// Standard ObjectARX entry point for (arxload ...)
extern "C" __attribute__((visibility("default")))
int acrxEntryPoint(int msg, void* pkt) {
    resolveArxSymbols();
    // kInitAppMsg = 1, kUnloadAppMsg = 2
    if (msg == 1) {
        if (g_fnRegisterAppMDIAware && pkt) {
            g_fnRegisterAppMDIAware(pkt);
        }
        startSocketServer();
        if (g_fnAcutPrintf) {
            g_fnAcutPrintf(L"\n[AutoCAD-MCP] Native bridge v0.2.0 listening on 127.0.0.1:9877\n");
        }
    } else if (msg == 2) {
        stopSocketServer();
    }
    return 0; // AcRx::kRetOK
}
