use crate::common::*;
use rusqlite::{Connection, OptionalExtension};

const TABLE: &str = "CREATE TABLE commerce_bindings (id TEXT PRIMARY KEY,shop_id TEXT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,key_digest TEXT UNIQUE NOT NULL,label TEXT NOT NULL,created REAL NOT NULL,expires REAL NOT NULL,revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)))";
const INDEX: &str = "CREATE INDEX commerce_bindings_shop ON commerce_bindings(shop_id,revoked)";

fn sql_shape(value: &str) -> String {
    value
        .chars()
        .filter(|character| !character.is_ascii_whitespace())
        .flat_map(char::to_lowercase)
        .collect()
}

/// Schema 23 only adds shop catalogue bindings; existing rows and keys are untouched.
pub(super) fn upgrade22(connection: &Connection) -> Result<()> {
    connection.execute_batch("BEGIN IMMEDIATE")?;
    let result = (|| {
        let version: i64 = connection.query_row("PRAGMA user_version", [], |row| row.get(0))?;
        if version == 23 {
            return Ok(());
        }
        if version != 22 {
            return Err(ApiError::new(409, "仅支持从 schema 22 升级到 schema 23"));
        }
        for (name, kind, expected) in [
            ("commerce_bindings", "table", TABLE),
            ("commerce_bindings_shop", "index", INDEX),
        ] {
            let present: Option<(String, Option<String>)> = connection
                .query_row(
                    "SELECT type,sql FROM sqlite_master WHERE name=?",
                    [name],
                    |row| Ok((row.get(0)?, row.get(1)?)),
                )
                .optional()?;
            if let Some((actual_kind, actual_sql)) = present {
                if actual_kind != kind
                    || actual_sql.as_deref().map(sql_shape) != Some(sql_shape(expected))
                {
                    return Err(ApiError::new(
                        409,
                        "商城绑定结构不兼容，请先恢复匹配的数据库",
                    ));
                }
            } else {
                connection.execute_batch(expected)?;
            }
        }
        connection.execute_batch("PRAGMA user_version=23")?;
        Ok(())
    })();
    match result {
        Ok(()) => {
            connection.execute_batch("COMMIT")?;
            Ok(())
        }
        Err(error) => {
            connection.execute_batch("ROLLBACK")?;
            Err(error)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn legacy() -> Connection {
        let connection = Connection::open_in_memory().unwrap();
        connection.execute_batch("PRAGMA foreign_keys=ON; CREATE TABLE shops(id TEXT PRIMARY KEY,name TEXT); CREATE TABLE cards(id TEXT PRIMARY KEY,content BLOB); INSERT INTO shops VALUES ('shop','retained shop'); INSERT INTO cards VALUES ('card',X'000102FF'); PRAGMA user_version=22;").unwrap();
        connection
    }

    fn schema(connection: &Connection) -> Vec<(String, String, String)> {
        connection
            .prepare(
                "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name",
            )
            .unwrap()
            .query_map([], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)))
            .unwrap()
            .map(|row| row.unwrap())
            .collect()
    }

    #[test]
    fn schema22_adds_only_bindings_and_preserves_every_existing_object_and_row() {
        let connection = legacy();
        let before = schema(&connection);
        upgrade22(&connection).unwrap();
        let after = schema(&connection);
        assert_eq!(after.len(), before.len() + 2);
        assert!(before.iter().all(|object| after.contains(object)));
        assert_eq!(
            connection
                .query_row::<i64, _, _>("PRAGMA user_version", [], |row| row.get(0))
                .unwrap(),
            23
        );
        assert_eq!(
            connection
                .query_row::<String, _, _>("SELECT name FROM shops", [], |row| row.get(0))
                .unwrap(),
            "retained shop"
        );
        assert_eq!(
            connection
                .query_row::<Vec<u8>, _, _>("SELECT content FROM cards", [], |row| row.get(0))
                .unwrap(),
            [0, 1, 2, 255]
        );
        assert_eq!(
            connection
                .query_row::<i64, _, _>("SELECT count(*) FROM commerce_bindings", [], |row| row
                    .get(0))
                .unwrap(),
            0
        );
        upgrade22(&connection).unwrap();
        assert_eq!(schema(&connection), after);
    }

    #[test]
    fn conflicting_index_rolls_back_the_new_table_and_version() {
        let connection = legacy();
        connection
            .execute_batch("CREATE INDEX commerce_bindings_shop ON shops(name)")
            .unwrap();
        let before = schema(&connection);
        assert!(upgrade22(&connection).is_err());
        assert_eq!(schema(&connection), before);
        assert_eq!(
            connection
                .query_row::<i64, _, _>("PRAGMA user_version", [], |row| row.get(0))
                .unwrap(),
            22
        );
    }

    #[test]
    fn unknown_schema_is_not_mutated() {
        let connection = legacy();
        connection.execute_batch("PRAGMA user_version=21").unwrap();
        let before = schema(&connection);
        assert!(upgrade22(&connection).is_err());
        assert_eq!(schema(&connection), before);
        assert_eq!(
            connection
                .query_row::<i64, _, _>("PRAGMA user_version", [], |row| row.get(0))
                .unwrap(),
            21
        );
    }
}
