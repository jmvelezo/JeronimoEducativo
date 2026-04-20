import os
from pathlib import Path

from werkzeug.security import generate_password_hash

from db import BASE_DIR, DATABASE, get_connection, init_db


def clear_upload_tree() -> None:
    uploads_root = BASE_DIR / "uploads"
    if not uploads_root.exists():
        return
    for folder in uploads_root.rglob("*"):
        if folder.is_file():
            folder.unlink()


def seed() -> None:
    init_db()
    clear_upload_tree()

    admin_user = os.environ.get("JEROCOIN_ADMIN_USER", "jerocoin_admin")
    admin_password = os.environ.get("JEROCOIN_ADMIN_PASSWORD", "JeroCoin-Admin-2026")

    with get_connection() as conn:
        for table in [
            "audit_log", "ai_assistant_messages", "contract_reviews", "deliveries", "contracts",
            "transactions", "rewards", "interventor_assignments", "cycle_custom_charges",
            "cycle_runs", "cycle_teams", "admin_offers", "team_gallery", "portfolios",
            "economic_settings", "cycles", "wallets", "users", "team_members", "students", "teams"
        ]:
            conn.execute(f"DELETE FROM {table}")
        conn.commit()

        conn.execute(
            "INSERT INTO users (username, password_hash, role, team_id, active) VALUES (?, ?, 'admin', NULL, 1)",
            (admin_user, generate_password_hash(admin_password)),
        )
        conn.execute(
            "INSERT INTO economic_settings (effective_from, base_cost, cost_per_member, second_project_surcharge, contract_price, required_interventor_signatures) VALUES (?, ?, ?, ?, ?, ?)",
            ("2026-04-20 00:00:00", 6, 5, 8, 30, 1),
        )
        conn.execute(
            "INSERT INTO wallets (owner_type, owner_id, balance) VALUES ('treasury', NULL, ?)",
            (1000,),
        )
        conn.commit()

    print(f"Base limpia creada correctamente en {DATABASE.name}")
    print(f"Usuario admin inicial: {admin_user}")
    print(f"Contraseña admin inicial: {admin_password}")


if __name__ == "__main__":
    seed()
