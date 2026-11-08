// Synthetic fixtures only. The real SecItemCopyMatching entry point is excluded
// from this test executable by HSL_TESTING; no keychain is accessed.
import CryptoKit
import Darwin
import Foundation
import Security

struct SyntheticTestFailure: Error {
    let label: String
}

@main
enum HomeKitExportTests {
    static func require(_ condition: Bool, _ label: String) throws {
        if !condition { throw SyntheticTestFailure(label: label) }
    }

    static func rejected(_ label: String, _ body: () throws -> Void) throws {
        do {
            try body()
        } catch is HomeKitExportError {
            return
        }
        throw SyntheticTestFailure(label: label)
    }

    static func fixtures() throws -> [[String: Any]] {
        let identity = try Curve25519.Signing.PrivateKey(rawRepresentation: Data((0..<32).map(UInt8.init)))
        return [
            [
                kSecAttrAccessGroup as String: homeKitAccessGroup,
                kSecAttrService as String: "HomeKit Pairing Identity",
                kSecAttrAccount as String: "synthetic-controller",
                kSecValueData as String: identity.publicKey.rawRepresentation + identity.rawRepresentation,
            ],
            [
                kSecAttrAccessGroup as String: homeKitAccessGroup,
                kSecAttrService as String: "Paired HomeKit Accessory: synthetic-accessory",
                kSecAttrAccount as String: "synthetic-accessory",
                kSecValueData as String: Data((32..<64).map(UInt8.init)),
            ],
        ]
    }

    static func testQuery() throws {
        var calls = 0
        let records = try queryHomeKitRecords { query in
            calls += 1
            let expected = query[kSecAttrAccessGroup as String] as? String == homeKitAccessGroup
                && query[kSecUseDataProtectionKeychain as String] as? Bool == true
                && query[kSecReturnAttributes as String] as? Bool == true
                && query[kSecReturnData as String] as? Bool == true
                && query[kSecClass as String] as? String == kSecClassGenericPassword as String
                && query[kSecAttrSynchronizable as String] as? String == kSecAttrSynchronizableAny as String
                && query[kSecMatchLimit as String] as? String == kSecMatchLimitAll as String
                && query.count == 7
            return expected ? (errSecSuccess, try? fixtures()) : (errSecParam, nil)
        }
        try require(calls == 1 && records.count == 2, "query must be restricted before returning secret data")
        for status in [errSecItemNotFound, errSecAuthFailed, errSecMissingEntitlement] {
            try rejected("keychain failures must stop export") {
                _ = try queryHomeKitRecords { _ in (status, nil) }
            }
        }
        try rejected("unexpected responses must stop export") {
            _ = try queryHomeKitRecords { _ in (errSecSuccess, "not an item list") }
        }
    }

    static func testRecords() throws {
        let original = try fixtures()
        let records = try homeKitRecords(from: original + original)
        try require(records.count == 2, "identical records must be deduplicated")
        let text = String(decoding: renderHomeKitRecords(records), as: UTF8.self)
        try require(text.contains("HomeKit Pairing Identity") && text.contains("Account: synthetic-controller"), "dump format")
        for index in original.indices {
            let expected = original[index][kSecValueData as String] as! Data
            let hex = expected.map { String(format: "%02x", $0) }.joined()
            try require(text.contains("Key: " + hex), "render full key bytes only into the private dump")
            var encoded = original
            encoded[index][kSecValueData as String] = Data(("0x" + hex).utf8)
            try require(try homeKitRecords(from: encoded).count == 2, "hexadecimal key compatibility")
        }
        for (attribute, value) in [
            (kSecAttrAccessGroup as String, "unrelated-group" as Any),
            (kSecAttrAccount as String, "invalid\naccount" as Any),
            (kSecAttrAccount as String, "" as Any),
            (kSecValueData as String, Data(repeating: 0, count: 64) as Any),
            (kSecValueData as String, Data("64 bytes (binary)".utf8) as Any),
        ] {
            var altered = original
            altered[0][attribute] = value
            try rejected("malformed or out-of-group records must not be rendered") {
                _ = try homeKitRecords(from: altered)
            }
        }
        var conflict = original[1]
        conflict[kSecValueData as String] = Data(repeating: 1, count: 32)
        try rejected("changed key for an existing identifier must stop export") {
            _ = try homeKitRecords(from: original + [conflict])
        }
        try rejected("missing controller must stop export") { _ = try homeKitRecords(from: [original[1]]) }
        try rejected("missing accessory must stop export") { _ = try homeKitRecords(from: [original[0]]) }
        try rejected("oversized responses must stop export") {
            _ = try homeKitRecords(from: Array(repeating: original[0], count: 513))
        }
        var unknown = original[0]
        unknown[kSecAttrService as String] = "Unrelated HomeKit service"
        try require(try homeKitRecords(from: original + [unknown]).count == 2, "ignore unknown HAP services")
    }

    static func testOutputPermissions() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: directory) }
        let path = directory.appendingPathComponent("synthetic-output")
        try Data().write(to: path)
        let file = try FileHandle(forUpdating: path)
        defer { try? file.close() }
        _ = fchmod(file.fileDescriptor, 0o600)
        try require(privateOutput(file.fileDescriptor) == (getuid() != 0), "accept private user-owned regular output")
        _ = fchmod(file.fileDescriptor, 0o644)
        try require(!privateOutput(file.fileDescriptor), "reject readable output")
        _ = fchmod(file.fileDescriptor, 0o600)
        try FileManager.default.linkItem(at: path, to: directory.appendingPathComponent("linked-output"))
        try require(!privateOutput(file.fileDescriptor), "reject hard-linked output")
        let pipe = Pipe()
        try require(!privateOutput(pipe.fileHandleForWriting.fileDescriptor), "reject pipes")
    }

    static func main() {
        do {
            try testQuery()
            try testRecords()
            try testOutputPermissions()
            if CommandLine.arguments.count == 2 {
                let destination = URL(fileURLWithPath: CommandLine.arguments[1])
                let data = renderHomeKitRecords(try homeKitRecords(from: fixtures()))
                try data.write(to: destination, options: .withoutOverwriting)
            }
            print("Native helper synthetic tests passed: scoped queries, key validation, failures and private output.")
        } catch let failure as SyntheticTestFailure {
            FileHandle.standardError.write(Data(("Synthetic test failed: " + failure.label + "\n").utf8))
            exit(1)
        } catch {
            FileHandle.standardError.write(Data("Synthetic helper tests failed without accessing a keychain.\n".utf8))
            exit(1)
        }
    }
}
