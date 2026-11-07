// reelvision: on-device OCR + image similarity for Reel Inbox, using Apple's Vision framework.
//
// Usage: reelvision <image> [<image> ...]
// Prints one JSON object to stdout:
//   {"images": [{"path": ..., "text": "...", "lines": [...], "chars": N}],
//    "distance": [[...]]}   // pairwise feature-print distance (0 = identical)
//
// Nothing leaves the Mac. Free.

import Foundation
import Vision
import CoreImage

struct Line: Codable { let text: String; let confidence: Float; let y: Double; let x: Double }
struct ImageResult: Codable { let path: String; let text: String; let lines: [Line]; let chars: Int; let error: String? }
struct Output: Codable { let images: [ImageResult]; let distance: [[Float]] }

func ocr(_ url: URL) -> ImageResult {
    let req = VNRecognizeTextRequest()
    req.recognitionLevel = .accurate
    // Language correction "fixes" code into English words, so leave it off.
    req.usesLanguageCorrection = false
    req.automaticallyDetectsLanguage = true
    let handler = VNImageRequestHandler(url: url, options: [:])
    do {
        try handler.perform([req])
    } catch {
        return ImageResult(path: url.path, text: "", lines: [], chars: 0, error: "\(error)")
    }
    var lines: [Line] = []
    for obs in req.results ?? [] {
        guard let top = obs.topCandidates(1).first else { continue }
        let t = top.string.trimmingCharacters(in: .whitespaces)
        if t.isEmpty || top.confidence < 0.3 { continue }
        let box = obs.boundingBox  // normalized, origin bottom-left
        lines.append(Line(text: t, confidence: top.confidence, y: 1 - box.maxY, x: box.minX))
    }
    // Reading order: top to bottom, then left to right (rows within ~1.5% height are one row).
    lines.sort { a, b in abs(a.y - b.y) < 0.015 ? a.x < b.x : a.y < b.y }
    let text = lines.map { $0.text }.joined(separator: "\n")
    return ImageResult(path: url.path, text: text, lines: lines, chars: text.count, error: nil)
}

func featurePrint(_ url: URL) -> VNFeaturePrintObservation? {
    let req = VNGenerateImageFeaturePrintRequest()
    let handler = VNImageRequestHandler(url: url, options: [:])
    try? handler.perform([req])
    return req.results?.first
}

let paths = CommandLine.arguments.dropFirst().map { URL(fileURLWithPath: $0) }
if paths.isEmpty {
    FileHandle.standardError.write("usage: reelvision <image> [<image> ...]\n".data(using: .utf8)!)
    exit(2)
}

let results = paths.map(ocr)
let prints = paths.map(featurePrint)
var dist = Array(repeating: Array(repeating: Float(0), count: paths.count), count: paths.count)
for i in 0..<paths.count {
    for j in (i + 1)..<max(i + 1, paths.count) {
        var d: Float = 99
        if let a = prints[i], let b = prints[j] { try? a.computeDistance(&d, to: b) }
        dist[i][j] = d; dist[j][i] = d
    }
}
let enc = JSONEncoder()
let data = try enc.encode(Output(images: results, distance: dist))
FileHandle.standardOutput.write(data)
