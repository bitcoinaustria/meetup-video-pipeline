#!/usr/bin/env swift

import Foundation
import Vision

guard CommandLine.arguments.count == 5,
      CommandLine.arguments[1] == "--list",
      CommandLine.arguments[3] == "--output" else {
    fputs("usage: vision-people.swift --list INPUT.tsv --output OUTPUT.tsv\n", stderr)
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
    let fields = row.split(separator: "\t", maxSplits: 1).map(String.init)
    guard fields.count == 2 else { continue }

    let request = VNDetectHumanRectanglesRequest()
    request.upperBodyOnly = true
    do {
        try VNImageRequestHandler(url: URL(fileURLWithPath: fields[1])).perform([request])
        let boxes = (request.results ?? [])
            .sorted { $0.boundingBox.midX < $1.boundingBox.midX }
            .map { box in
                String(format: "%.5f,%.5f,%.5f,%.5f", box.boundingBox.minX, box.boundingBox.minY, box.boundingBox.width, box.boundingBox.height)
            }
        output.append("\(fields[0])\t\(boxes.joined(separator: ";"))")
    } catch {
        fputs("detection failed for \(fields[1]): \(error)\n", stderr)
        output.append("\(fields[0])\t")
    }
}

do {
    try (output.joined(separator: "\n") + "\n").write(to: outputURL, atomically: true, encoding: .utf8)
    print("\(output.count) detection results -> \(outputURL.path)")
} catch {
    fputs("cannot write \(outputURL.path): \(error)\n", stderr)
    exit(1)
}
