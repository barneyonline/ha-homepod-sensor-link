// HomePod Sensor Link's user-run, read-only HomeKit credential export helper.
// Built only from Apple frameworks. No package resolution or automatic signing.
import CryptoKit
import Darwin
import Foundation
import Security

let homeKitAccessGroup = "com.apple.hap.pairing"

enum HomeKitExportError: Error {
    case keychain(OSStatus)
    case invalidResponse
    case invalidRecord
    case conflictingRecord
    case noRecords
    case unsafeOutput

    var message: String {
        switch self {
        case .keychain(let status):
            return "HomeKit query failed (OSStatus \(status)). No credential values were printed."
        case .invalidResponse:
            return "Unexpected HomeKit query response. Export stopped."
        case .invalidRecord:
            return "A HomeKit record has an unsupported identifier, key format or key pair. Export stopped."
        case .conflictingRecord:
            return "Conflicting HomeKit records share an identifier. Export stopped."
        case .noRecords:
            return "No usable HomeKit controller and accessory records were found."
        case .unsafeOutput:
            return "Redirect export to a private regular file owned by you with permissions 0600; do not pipe or display keys."
        }
    }
}

struct HomeKitExportRecord {
    enum Kind: String {
        case controller
        case accessory
    }

    let kind: Kind
    let account: String
    let key: Data
}

func homeKitQuery() -> [String: Any] {
    // Data protection is explicit: macOS legacy keychains may ignore access groups.
    return [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrAccessGroup as String: homeKitAccessGroup,
        kSecUseDataProtectionKeychain as String: true,
        kSecAttrSynchronizable as String: kSecAttrSynchronizableAny,
        kSecReturnAttributes as String: true,
        kSecReturnData as String: true,
        kSecMatchLimit as String: kSecMatchLimitAll,
    ]
}

func decodeHomeKitKey(_ value: Data, byteCount: Int) throws -> Data {
    if value.count == byteCount {
        return value
    }
    guard var text = String(data: value, encoding: .utf8) else {
        throw HomeKitExportError.invalidRecord
    }
    text = text.trimmingCharacters(in: .whitespacesAndNewlines)
    text = text.trimmingCharacters(in: CharacterSet(charactersIn: "\"“”"))
    if text.hasPrefix("0x") {
        text = String(text.dropFirst(2))
    }
    text = text.replacingOccurrences(of: "+", with: "")
    let characters = Array(text.utf8)
    guard characters.count == byteCount * 2,
          characters.allSatisfy({ (48...57).contains($0) || (65...70).contains($0) || (97...102).contains($0) }) else {
        throw HomeKitExportError.invalidRecord
    }
    var bytes = Data()
    for offset in stride(from: 0, to: characters.count, by: 2) {
        guard let byte = UInt8(String(decoding: characters[offset..<offset + 2], as: UTF8.self), radix: 16) else {
            throw HomeKitExportError.invalidRecord
        }
        bytes.append(byte)
    }
    return bytes
}

func homeKitRecords(from items: [[String: Any]]) throws -> [HomeKitExportRecord] {
    guard items.count <= 512 else {
        throw HomeKitExportError.invalidResponse
    }
    var records: [String: HomeKitExportRecord] = [:]
    for item in items {
        guard item[kSecAttrAccessGroup as String] as? String == homeKitAccessGroup else {
            throw HomeKitExportError.invalidResponse
        }
        let names = [item[kSecAttrService as String], item[kSecAttrLabel as String]].compactMap { $0 as? String }
        let kind: HomeKitExportRecord.Kind
        if names.contains("HomeKit Pairing Identity") {
            kind = .controller
        } else if names.contains(where: { $0.hasPrefix("Paired HomeKit Accessory:") }) {
            kind = .accessory
        } else {
            continue
        }
        guard let account = item[kSecAttrAccount as String] as? String,
              !account.isEmpty, account.count <= 256,
              !account.unicodeScalars.contains(where: { CharacterSet.controlCharacters.contains($0) }),
              let value = item[kSecValueData as String] as? Data else {
            throw HomeKitExportError.invalidRecord
        }
        let key = try decodeHomeKitKey(value, byteCount: kind == .controller ? 64 : 32)
        if kind == .controller {
            let identity: Curve25519.Signing.PrivateKey
            do {
                identity = try Curve25519.Signing.PrivateKey(rawRepresentation: Data(key.suffix(32)))
            } catch {
                throw HomeKitExportError.invalidRecord
            }
            guard identity.publicKey.rawRepresentation == Data(key.prefix(32)) else {
                throw HomeKitExportError.invalidRecord
            }
        }
        let identifier = kind.rawValue + ":" + account
        if let existing = records[identifier], existing.key != key {
            throw HomeKitExportError.conflictingRecord
        }
        records[identifier] = HomeKitExportRecord(kind: kind, account: account, key: key)
    }
    let result = records.values.sorted { ($0.kind.rawValue, $0.account) < ($1.kind.rawValue, $1.account) }
    guard result.contains(where: { $0.kind == .controller }), result.contains(where: { $0.kind == .accessory }) else {
        throw HomeKitExportError.noRecords
    }
    return result
}

typealias HomeKitItemReader = ([String: Any]) -> (OSStatus, Any?)

func queryHomeKitRecords(using reader: HomeKitItemReader) throws -> [HomeKitExportRecord] {
    let (status, result) = reader(homeKitQuery())
    if status == errSecItemNotFound {
        throw HomeKitExportError.noRecords
    }
    guard status == errSecSuccess else {
        throw HomeKitExportError.keychain(status)
    }
    guard let items = result as? [[String: Any]] else {
        throw HomeKitExportError.invalidResponse
    }
    return try homeKitRecords(from: items)
}

func renderHomeKitRecords(_ records: [HomeKitExportRecord]) -> Data {
    // Validate every record before rendering anything. Do not log raw API errors.
    let text = records.map { record in
        let name = record.kind == .controller ? "HomeKit Pairing Identity" : "Paired HomeKit Accessory"
        let key = record.key.map { String(format: "%02x", $0) }.joined()
        return "\(name) (\(homeKitAccessGroup))\nAccount: \(record.account)\nKey: \(key)\n"
    }.joined(separator: "\n")
    return Data(text.utf8)
}

func privateOutput(_ descriptor: Int32) -> Bool {
    var info = stat()
    return fstat(descriptor, &info) == 0
        && (info.st_mode & S_IFMT) == S_IFREG
        && (info.st_mode & 0o777) == 0o600
        && info.st_uid == getuid()
        && getuid() != 0
        && info.st_nlink == 1
}

#if !HSL_TESTING
@main
enum HomeKitExport {
    static func main() {
        let arguments = Array(CommandLine.arguments.dropFirst())
        if arguments == ["--help"] {
            print("Usage: homekit-export --export-hap > private-file")
            print("Run only through the guided wizard after its security checks and signing confirmation.")
            print("--help and --describe-query never access a keychain.")
            return
        }
        if arguments == ["--describe-query"] {
            print("Read-only generic-password query for com.apple.hap.pairing in the data-protection keychain.")
            print("Includes synchronizable and local records. No wildcard groups, writes or legacy-keychain fallback.")
            return
        }
        guard arguments == ["--export-hap"] else {
            FileHandle.standardError.write(Data("Use --help. No keychain was accessed.\n".utf8))
            exit(2)
        }
        do {
            guard privateOutput(STDOUT_FILENO) else {
                throw HomeKitExportError.unsafeOutput
            }
            let records = try queryHomeKitRecords { query in
                var result: CFTypeRef?
                let status = SecItemCopyMatching(query as CFDictionary, &result)
                return (status, result)
            }
            FileHandle.standardOutput.write(renderHomeKitRecords(records))
        } catch let error as HomeKitExportError {
            FileHandle.standardError.write(Data((error.message + " Restore protections before troubleshooting.\n").utf8))
            exit(1)
        } catch {
            FileHandle.standardError.write(Data("Export failed. Restore protections before troubleshooting.\n".utf8))
            exit(1)
        }
    }
}
#endif
