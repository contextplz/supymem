// QA-only recipient monitor. Never records keys, text, coordinates or screen data.
import AppKit
import CryptoKit

@objc(DecoyApplication)
final class DecoyApplication: NSApplication {
    var received = 0
    override func sendEvent(_ event: NSEvent) {
        switch event.type {
        case .keyDown, .keyUp, .flagsChanged, .leftMouseDown, .leftMouseUp,
             .rightMouseDown, .rightMouseUp, .otherMouseDown, .otherMouseUp,
             .leftMouseDragged, .rightMouseDragged, .otherMouseDragged, .scrollWheel:
            received += 1
        default: break
        }
        super.sendEvent(event)
    }
}

final class Delegate: NSObject, NSApplicationDelegate {
    let state: URL
    let instance: String
    var window: NSWindow?
    var field: NSTextView?
    var timer: Timer?
    var samples = 0
    init(state: URL, instance: String) { self.state = state; self.instance = instance }
    func applicationDidFinishLaunching(_ notification: Notification) {
        let window = NSWindow(contentRect: NSRect(x: 200, y: 200, width: 460, height: 240),
                              styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.title = "Supymem QA input monitor"
        let field = NSTextView(frame: NSRect(x: 0, y: 0, width: 460, height: 240))
        field.string = "QA decoy — keep this window frontmost. Input is counted without recording characters."
        window.contentView = field
        self.field = field
        self.window = window
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        snapshot()
        timer = Timer.scheduledTimer(withTimeInterval: 0.2, repeats: true) { [weak self] _ in self?.snapshot() }
    }
    func snapshot() {
        samples += 1
        let data: [String: Any] = ["instance_id": instance, "pid": ProcessInfo.processInfo.processIdentifier,
                                   "received_input_events": (NSApp as? DecoyApplication)?.received ?? -1,
                                   "samples": samples, "updated_at": Date().timeIntervalSince1970,
                                   "monotonic_at": ProcessInfo.processInfo.systemUptime,
                                   "is_active": NSApp.isActive,
                                   "active_app_pid": NSWorkspace.shared.frontmostApplication?.processIdentifier ?? -1,
                                   "body_sha256": SHA256.hash(data: Data((field?.string ?? "").utf8)).map { String(format: "%02x", $0) }.joined()]
        do {
            let bytes = try JSONSerialization.data(withJSONObject: data)
            try bytes.write(to: state, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: state.path)
        } catch { NSApp.terminate(nil) } // Missing evidence is never success.
    }
}

if CommandLine.arguments.count != 3 { exit(2) }
// Use AppKit's factory singleton. Direct initialization creates a second
// application when accessibility lazily calls sharedApplication.
let app = DecoyApplication.shared
let delegate = Delegate(state: URL(fileURLWithPath: CommandLine.arguments[1]), instance: CommandLine.arguments[2])
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
