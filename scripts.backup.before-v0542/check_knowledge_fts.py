"""Check local SQLite FTS5 trigram capability, without touching the actual DB."""
import sqlite3


def main():
    con = sqlite3.connect(':memory:')
    print('SQLite:', sqlite3.sqlite_version)
    try:
        con.execute("CREATE VIRTUAL TABLE test_fts USING fts5(content, tokenize='trigram')")
        con.execute('INSERT INTO test_fts(content) VALUES(?)', ('繁體中文附件測試',))
        matched = con.execute("SELECT count(*) FROM test_fts WHERE test_fts MATCH ?", ('"中文附件"',)).fetchone()[0]
        print('✅ FTS5 trigram 可用，中文測試命中：', matched)
    except sqlite3.Error as e:
        print('⚠️ FTS5 trigram 不可用；搜尋將改用較慢的子字串查詢：', e)
    finally:
        con.close()


if __name__ == '__main__':
    main()
