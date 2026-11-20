"""Database queries for the canteen menu and orders (database/canteen.db)."""

import sqlite3 as sql

DATABASE = "database/canteen.db"


def menu_get(category):
    """Return the menu items in a category ("%" means every category).

    README Step 6 changes this function.
    """
    with sql.connect(DATABASE) as con:
        cur = con.cursor()
        cur.execute("SELECT * FROM menu WHERE category LIKE ?;", [category])
        return [
            dict(
                itemID=row[0],
                name=row[1],
                description=row[2],
                price=row[3],
                category=row[4],
                emoji=row[5],
            )
            for row in cur.fetchall()
        ]


def order_add(name, item_ids, total):
    """Save a placed order in the database and return its order number."""
    with sql.connect(DATABASE) as con:
        cur = con.cursor()
        cur.execute(
            "INSERT INTO orders (name, items, total) VALUES (?, ?, ?);",
            [name, ",".join(str(item_id) for item_id in item_ids), total],
        )
        con.commit()
        return cur.lastrowid
