// Apple Vision OCR helper. Usage: apple_vision_ocr <lang,lang,...> <image>...
// Prints one JSON object per image; language correction is disabled.
import AppKit
import Foundation
import Vision

func recognize(_ path: String, _ languages: [String]) -> [String: Any] {
    guard let image = NSImage(contentsOfFile: path), let cg = image.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        return ["image": path, "error": "cannot load image"]
    }
    var lines: [[String: Any]] = []
    let request = VNRecognizeTextRequest { req, _ in
        for observation in (req.results as? [VNRecognizedTextObservation] ?? []) {
            guard let top = observation.topCandidates(1).first else { continue }
            let box = observation.boundingBox
            lines.append(["text": top.string, "confidence": top.confidence,
                          "box": [box.origin.x, box.origin.y, box.size.width, box.size.height]])
        }
    }
    request.recognitionLevel = .accurate
    request.usesLanguageCorrection = false
    request.recognitionLanguages = languages
    request.minimumTextHeight = 0
    do { try VNImageRequestHandler(cgImage: cg, options: [:]).perform([request]) }
    catch { return ["image": path, "error": "\(error)"] }
    return ["image": path, "languages": languages, "revision": request.revision, "lines": lines]
}

let args = CommandLine.arguments
if args.count < 3 {
    FileHandle.standardError.write("usage: apple_vision_ocr <lang,lang> <image>...\n".data(using: .utf8)!)
    exit(2)
}
let languages = args[1].split(separator: ",").map(String.init)
for path in args.dropFirst(2) {
    let result = recognize(path, languages)
    let data = try! JSONSerialization.data(withJSONObject: result)
    print(String(data: data, encoding: .utf8)!)
}
