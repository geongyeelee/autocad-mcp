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

static FnRegisterAppMDIAware g_fnRegisterAppMDIAware = nullptr;
static FnPostCommand g_fnPostCommand = nullptr;
static FnEvaluateLisp g_fnEvaluateLisp = nullptr;
static FnAcutRelRb g_fnAcutRelRb = nullptr;
static FnAcutPrintf g_fnAcutPrintf = nullptr;

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
        // Binary is inside .../bridge/autocad_mcp_bridge.crx/Versions/A/autocad_mcp_bridge
        // or .../bridge/autocad_mcp_bridge.bundle/Contents/MacOS/autocad_mcp_bridge
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
            // Fallback to postCommandString
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

// Check if AutoCAD has an active drawing document (not just the Start tab)
static bool ensureActiveDrawingOnMainThread() {
    NSDocumentController* dc = [NSDocumentController sharedDocumentController];
    if (!dc) return false;
    NSArray* docs = [dc documents];
    for (id doc in docs) {
        SEL isStartSel = NSSelectorFromString(@"isStartTabDoc");
        if ([doc respondsToSelector:isStartSel]) {
            BOOL isStart = ((BOOL (*)(id, SEL))objc_msgSend)(doc, isStartSel);
            if (!isStart) {
                return true;
            }
        } else {
            return true;
        }
    }
    // No non-start document open: create an untitled drawing document
    NSError* err = nil;
    id newDoc = [dc openUntitledDocumentAndDisplay:YES error:&err];
    return (newDoc != nil);
}

static std::string executeLispViaCommandQueue(const std::string& lispCode, int timeoutMs = 15000) {
    std::lock_guard<std::mutex> lock(g_execMutex);
    resolveArxSymbols();

    uint64_t seq = ++g_reqCounter;
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

    // Build a self-bootstrapping AutoLISP command that loads mcp_helpers.lsp if needed
    // and executes the request file, writing JSON output to respPath.
    std::ostringstream cmdBuilder;
    cmdBuilder << "(progn "
               << "(if (not mcp:exec-file) (load \"" << helpersPath << "\")) "
               << "(mcp:exec-file \"" << reqPath << "\" \"" << respPath << "\")"
               << ")\n";
    std::string cmdUtf8 = cmdBuilder.str();
    std::wstring cmdW = utf8ToWstring(cmdUtf8);

    __block bool posted = false;
    __block bool directSuccess = false;
    __block std::string directResultJson;

    dispatch_sync(dispatch_get_main_queue(), ^{
        @autoreleasepool {
            ensureActiveDrawingOnMainThread();
            if (g_fnPostCommand) {
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
        }
    });

    if (!posted) {
        unlink(reqPath.c_str());
        return "{\"ok\":false,\"error\":\"Failed to post command to AutoCAD main thread\"}";
    }

    // Wait for respPath to appear
    auto start = std::chrono::steady_clock::now();
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
                return content;
            }
        }
        auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
            std::chrono::steady_clock::now() - start).count();
        if (elapsed >= timeoutMs) {
            break;
        }
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }

    unlink(reqPath.c_str());
    unlink(respPath.c_str());
    unlink(respTmpPath.c_str());
    return "{\"ok\":false,\"error\":\"Timed out waiting for AutoCAD LISP execution response\"}";
}

static std::string postRawCommand(const std::string& rawCmd) {
    resolveArxSymbols();
    std::string cmdWithNewline = rawCmd;
    if (cmdWithNewline.empty() || cmdWithNewline.back() != '\n') {
        cmdWithNewline.push_back('\n');
    }
    std::wstring cmdW = utf8ToWstring(cmdWithNewline);

    __block bool ok = false;
    dispatch_sync(dispatch_get_main_queue(), ^{
        @autoreleasepool {
            ensureActiveDrawingOnMainThread();
            if (g_fnPostCommand) {
                ok = (g_fnPostCommand(cmdW.c_str()) != 0);
            }
        }
    });

    if (ok) {
        return "{\"ok\":true,\"status\":\"Command posted to AutoCAD command line\"}";
    }
    return "{\"ok\":false,\"error\":\"Failed to post raw command to AutoCAD\"}";
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
                // Extract serialized id value
                std::string idJson = "null";
                if (idWrapper) {
                    std::string w = [idWrapper UTF8String];
                    size_t colon = w.find(':');
                    size_t endBrace = w.rfind('}');
                    if (colon != std::string::npos && endBrace != std::string::npos && endBrace > colon + 1) {
                        idJson = w.substr(colon + 1, endBrace - colon - 1);
                    }
                }

                if ([method isEqualToString:@"ping"]) {
                    std::string pong = "{\"jsonrpc\":\"2.0\",\"result\":{\"ok\":true,\"app\":\"AutoCAD 2027\",\"bridge_version\":\"0.1.0\"},\"id\":" + idJson + "}\n";
                    send(clientSock, pong.data(), pong.size(), 0);
                    continue;
                }

                if ([method isEqualToString:@"eval_lisp"]) {
                    NSString* code = params[@"code"] ?: @"";
                    int timeoutMs = params[@"timeout_ms"] ? [params[@"timeout_ms"] intValue] : 15000;
                    std::string resPayload = executeLispViaCommandQueue([code UTF8String], timeoutMs);
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
            g_fnAcutPrintf(L"\n[AutoCAD-MCP] Native bridge listening on 127.0.0.1:9877\n");
        }
    } else if (msg == 2) {
        stopSocketServer();
    }
    return 0; // AcRx::kRetOK
}
