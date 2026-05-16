import Database from "better-sqlite3";
import path from "path";

const DB_PATH = path.resolve(process.cwd(), "..", "data", "signalcatcher.db");

let _db: Database.Database | null = null;

export function getDb(): Database.Database {
  if (!_db) {
    _db = new Database(DB_PATH, { readonly: true });
    _db.pragma("journal_mode = WAL");
  }
  return _db;
}

export function kstDateToUtcRange(dateStr: string): [string, string] {
  const [y, m, d] = dateStr.split("-").map(Number);
  const kstStart = new Date(Date.UTC(y, m - 1, d, -9));
  const kstEnd = new Date(kstStart.getTime() + 86400000);
  const fmt = (dt: Date) =>
    dt.toISOString().replace("Z", "").split(".")[0];
  return [fmt(kstStart), fmt(kstEnd)];
}
