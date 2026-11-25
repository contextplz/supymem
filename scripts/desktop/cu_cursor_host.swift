// Dev/QA overlay host for the Supymem agent cursor (the shipped one lives in src-tauri/src/agent_cursor.rs).
// Reuses src-tauri/splashscreen/agent-cursor and the same datagram event schema.
//   swiftc -O scripts/desktop/cu_cursor_host.swift -o /tmp/cu_cursor_host
//   /tmp/cu_cursor_host <socket-path> <page-url>      (serve splashscreen/ over http; file:// blocks ES modules)
// Click-through, non-activating, accessory app: it can never take focus or receive input.
import Cocoa
import WebKit

let args = CommandLine.arguments
guard args.count == 3 else { FileHandle.standardError.write("usage: host <socket> <url>\n".data(using: .utf8)!); exit(2) }
let sockPath = args[1], pageURL = URL(string: args[2])!

final class Overlay: NSPanel {
    override var canBecomeKey: Bool { false }
    override var canBecomeMain: Bool { false }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
var views: [WKWebView] = []
let primaryHeight = NSScreen.screens.first?.frame.height ?? 0
for screen in NSScreen.screens {
    let f = screen.frame
    let panel = Overlay(contentRect: f, styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
    panel.isOpaque = false; panel.backgroundColor = .clear; panel.hasShadow = false
    panel.ignoresMouseEvents = true; panel.level = .screenSaver; panel.sharingType = .none
    panel.hidesOnDeactivate = false; panel.canHide = false
    panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle, .stationary]
    let web = WKWebView(frame: NSRect(origin: .zero, size: f.size), configuration: WKWebViewConfiguration())
    web.setValue(false, forKey: "drawsBackground")
    let ox = f.minX, oy = primaryHeight - f.maxY
    let boot = WKUserScript(source: "window.__agentCursorInit=[\(ox),\(oy),\(f.width),\(f.height)]",
                            injectionTime: .atDocumentStart, forMainFrameOnly: true)
    web.configuration.userContentController.addUserScript(boot)
    web.load(URLRequest(url: pageURL))
    panel.contentView = web
    panel.orderFrontRegardless()
    views.append(web)
}

let fd = socket(AF_UNIX, SOCK_DGRAM, 0)
unlink(sockPath)
var addr = sockaddr_un(); addr.sun_family = sa_family_t(AF_UNIX)
withUnsafeMutablePointer(to: &addr.sun_path) { p in
    p.withMemoryRebound(to: CChar.self, capacity: 104) { _ = strncpy($0, sockPath, 103) }
}
let bound = withUnsafePointer(to: &addr) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { bind(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) } }
guard bound == 0 else { FileHandle.standardError.write("bind failed\n".data(using: .utf8)!); exit(1) }
chmod(sockPath, 0o600)

DispatchQueue.global().async {
    var buf = [UInt8](repeating: 0, count: 4096)
    while true {
        let n = recv(fd, &buf, buf.count, 0)
        if n <= 0 { continue }
        guard let json = String(bytes: buf[0..<n], encoding: .utf8) else { continue }
        if let log = ProcessInfo.processInfo.environment["CU_CURSOR_LOG"] {
            if let h = FileHandle(forWritingAtPath: log) ?? { FileManager.default.createFile(atPath: log, contents: nil); return FileHandle(forWritingAtPath: log) }() {
                h.seekToEndOfFile(); h.write((json + "\n").data(using: .utf8)!); h.closeFile()
            }
        }
        DispatchQueue.main.async {
            for v in views { v.evaluateJavaScript("window.__agentCursor&&window.__agentCursor.event(\(json))", completionHandler: nil) }
        }
    }
}
app.run()
