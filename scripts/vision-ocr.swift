#!/usr/bin/env swift

import Foundation
import Vision

guard CommandLine.arguments.count == 5,
      CommandLine.arguments[1] == "--list",
      CommandLine.arguments[3] == "--output" else {
    fputs("usage: vision-ocr.swift --list INPUT.tsv --output OUTPUT.tsv\n", stderr)
    exit(2)
}

let inputURL = URL(fileURLWithPath: CommandLine.arguments[2])
let outputURL = URL(fileURLWithPath: CommandLine.arguments[4])
guard let input = try? String(contentsOf: inputURL, encoding: .utf8) else {
    fputs("cannot read \(inputURL.path)\n", stderr)
    exit(2)
}

var output: [String] = []
for row in input.split(separator: "\n") {
    let fields = row.split(separator: "\t", maxSplits: 2).map(String.init)
    guard fields.count == 3 else { continue }
    let path = fields[2]
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .fast
    request.recognitionLanguages = ["en-US"]
    request.usesLanguageCorrection = true

    do {
        try VNImageRequestHandler(url: URL(fileURLWithPath: path)).perform([request])
        let lines = (request.results ?? [])
            .sorted { left, right in
                abs(left.boundingBox.maxY - right.boundingBox.maxY) > 0.02
                    ? left.boundingBox.maxY > right.boundingBox.maxY
                    : left.boundingBox.minX < right.boundingBox.minX
            }
            .compactMap { $0.topCandidates(1).first?.string }
        output.append("\(fields[0])\t\(fields[1])\t\(URL(fileURLWithPath: path).lastPathComponent)\t\(lines.joined(separator: " | "))")
    } catch {
        fputs("OCR failed for \(path): \(error)\n", stderr)
    }
}

do {
    try (output.joined(separator: "\n") + "\n").write(to: outputURL, atomically: true, encoding: .utf8)
    print("\(output.count) OCR results -> \(outputURL.path)")
} catch {
    fputs("cannot write \(outputURL.path): \(error)\n", stderr)
    exit(1)
}
