// icloudread: print an iCloud Drive file's contents, downloading it first if it's only a placeholder.
// Files saved on the iPhone arrive on the Mac as "dataless" placeholders; a plain read from a
// background process hangs or fails (EDEADLK). NSFileCoordinator asks iCloud to fetch it properly.
import Foundation
let url = URL(fileURLWithPath: CommandLine.arguments[1])
try? FileManager.default.startDownloadingUbiquitousItem(at: url)
var err: NSError?
var data: Data?
NSFileCoordinator().coordinate(readingItemAt: url, options: [], error: &err) { u in data = try? Data(contentsOf: u) }
guard let d = data else { FileHandle.standardError.write("read failed: \(String(describing: err))\n".data(using: .utf8)!); exit(1) }
FileHandle.standardOutput.write(d)
