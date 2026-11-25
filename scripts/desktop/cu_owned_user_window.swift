// QA-only owned user-window fixture. Commands cannot address another app.
// Programmatic text insertion is explicit; it is not hardware typing proof.
import AppKit
import CryptoKit

@objc(UserWindowApplication)
final class UserWindowApplication: NSApplication {
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
    let state: URL, control: URL, instance: String, slot: Int
    var window: NSWindow?, field: NSTextView?, timer: Timer?
    var samples = 0, commandSequence = 0, insertions = 0
    var status = "starting", pendingSelection = false
    init(state: URL, control: URL, instance: String, slot: Int) {
        self.state = state; self.control = control; self.instance = instance; self.slot = slot
    }
    func applicationDidFinishLaunching(_ notification: Notification) {
        let window = NSWindow(contentRect: NSRect(x: 60 + slot * 500, y: 240, width: 440, height: 220),
                              styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.title = slot == 0 ? "Supymem owned user fixture A" : "Supymem owned user fixture B"
        let field = NSTextView(frame: NSRect(x: 0, y: 0, width: 440, height: 220))
        field.string = "Owned user-window coexistence fixture."
        window.contentView = field
        self.field = field; self.window = window
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true) // Setup/explicit fixture selection only.
        status = "ready"
        snapshot()
        timer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in self?.tick() }
    }
    func tick() {
        if let bytes = try? Data(contentsOf: control), bytes.count <= 1024,
           let command = try? JSONSerialization.jsonObject(with: bytes) as? [String: Any],
           command["instance_id"] as? String == instance,
           let sequence = command["sequence"] as? Int, sequence > commandSequence, sequence <= 200,
           let issued = command["issued_at"] as? Double,
           Date().timeIntervalSince1970 - issued >= 0, Date().timeIntervalSince1970 - issued <= 2,
           let action = command["action"] as? String, ["select", "insert_fixture"].contains(action) {
            commandSequence = sequence // Consume once before effects; never replay on uncertainty.
            pendingSelection = false
            if action == "select" {
                status = "selection_pending"; pendingSelection = true
                window?.makeKeyAndOrderFront(nil)
                NSApp.activate(ignoringOtherApps: true)
            } else if NSApp.isActive && NSWorkspace.shared.frontmostApplication?.processIdentifier == ProcessInfo.processInfo.processIdentifier {
                let length = field?.string.utf16.count ?? 0
                field?.insertText("q", replacementRange: NSRange(location: length, length: 0))
                insertions += 1; status = "fixture_inserted"
            } else {
                status = "refused_not_selected"
            }
        }
        if pendingSelection && NSApp.isActive &&
            NSWorkspace.shared.frontmostApplication?.processIdentifier == ProcessInfo.processInfo.processIdentifier {
            pendingSelection = false; status = "selected"
        }
        snapshot()
    }
    func snapshot() {
        samples += 1
        let pointer = NSEvent.mouseLocation
        let hash: (String) -> String = { SHA256.hash(data: Data($0.utf8)).map { String(format: "%02x", $0) }.joined() }
        let data: [String: Any] = ["instance_id": instance, "pid": ProcessInfo.processInfo.processIdentifier,
            "received_input_events": (NSApp as? UserWindowApplication)?.received ?? -1,
            "samples": samples, "updated_at": Date().timeIntervalSince1970,
            "monotonic_at": ProcessInfo.processInfo.systemUptime,
            "is_active": NSApp.isActive,
            "active_app_pid": NSWorkspace.shared.frontmostApplication?.processIdentifier ?? -1,
            "body_sha256": hash(field?.string ?? ""), "pointer_sha256": hash("\(pointer.x),\(pointer.y)"),
            "fixture_insertions": insertions, "command_sequence": commandSequence, "command_status": status]
        do {
            try JSONSerialization.data(withJSONObject: data).write(to: state, options: .atomic)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: state.path)
        } catch { NSApp.terminate(nil) } // Missing evidence is never a pass.
    }
}

if CommandLine.arguments.count != 5 { exit(2) }
guard let slot = Int(CommandLine.arguments[4]), slot == 0 || slot == 1 else { exit(2) }
let app = UserWindowApplication.shared
let delegate = Delegate(state: URL(fileURLWithPath: CommandLine.arguments[1]),
                        control: URL(fileURLWithPath: CommandLine.arguments[2]),
                        instance: CommandLine.arguments[3], slot: slot)
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
