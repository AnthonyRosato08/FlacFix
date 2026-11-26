// seticon <cover-image> <file>...  — gives each file a Finder icon that looks like an image thumbnail.
import AppKit
import Foundation

let args = CommandLine.arguments
guard args.count >= 3, let src = NSImage(contentsOfFile: args[1]) else {
    print("usage: seticon cover.jpg file..."); exit(2)
}
let side: CGFloat = 512, inset: CGFloat = 30, radius: CGFloat = 22
let art = NSRect(x: inset, y: inset, width: side - 2 * inset, height: side - 2 * inset)
let image = NSImage(size: NSSize(width: side, height: side))
image.lockFocus()
NSGraphicsContext.current?.imageInterpolation = .high
NSBezierPath(roundedRect: art, xRadius: radius, yRadius: radius).addClip()
src.draw(in: art, from: .zero, operation: .sourceOver, fraction: 1)
image.unlockFocus()

var ok = 0, fail = 0
for f in args[2...] {
    var done = false
    for _ in 0..<6 {
        if NSWorkspace.shared.setIcon(image, forFile: f, options: []) { done = true; break }
        Thread.sleep(forTimeInterval: 0.4)
    }
    if done { ok += 1 } else { fail += 1; FileHandle.standardError.write(Data("icon failed: \(f)\n".utf8)) }
}
print("set \(ok) failed \(fail)")
exit(fail == 0 ? 0 : 1)
