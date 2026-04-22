PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS students (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT NOT NULL,
    course TEXT,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    team_type TEXT NOT NULL CHECK(team_type IN ('robotica', 'desarrollo')),
    service_track TEXT NOT NULL DEFAULT 'robotica' CHECK(service_track IN ('robotica', 'programacion', 'web_html')),
    course_label TEXT,
    market_role TEXT NOT NULL DEFAULT 'both' CHECK(market_role IN ('client_only', 'provider_only', 'both')),
    active INTEGER NOT NULL DEFAULT 1,
    max_contracts INTEGER NOT NULL DEFAULT 2,
    notes TEXT,
    profile_blurb TEXT,
    logo_original_filename TEXT,
    logo_stored_filename TEXT,
    google_sheet_url TEXT
);

CREATE TABLE IF NOT EXISTS team_members (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id INTEGER NOT NULL,
    student_id INTEGER NOT NULL,
    internal_role TEXT NOT NULL,
    joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
    active INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE,
    FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS team_gallery (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id INTEGER NOT NULL,
    caption TEXT,
    original_filename TEXT,
    stored_filename TEXT NOT NULL UNIQUE,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS team_sites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id INTEGER NOT NULL UNIQUE,
    slug TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'published' CHECK(status IN ('draft', 'published')),
    draft_html TEXT,
    draft_css TEXT,
    published_html TEXT,
    published_css TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS team_point_adjustments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id INTEGER NOT NULL,
    cycle_id INTEGER,
    category TEXT NOT NULL DEFAULT 'other' CHECK(category IN ('participation', 'behavior', 'other')),
    points_delta INTEGER NOT NULL,
    reason TEXT,
    created_by_user_id INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE,
    FOREIGN KEY (cycle_id) REFERENCES cycles(id) ON DELETE SET NULL,
    FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin', 'robotica_team', 'desarrollo_team', 'interventor')),
    team_id INTEGER,
    active INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS wallets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_type TEXT NOT NULL CHECK(owner_type IN ('team', 'treasury')),
    owner_id INTEGER,
    balance INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    start_date TEXT,
    end_date TEXT,
    status TEXT NOT NULL CHECK(status IN ('open', 'closed')) DEFAULT 'open',
    started INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS cycle_teams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id INTEGER NOT NULL,
    team_id INTEGER NOT NULL,
    team_type_snapshot TEXT NOT NULL CHECK(team_type_snapshot IN ('robotica', 'desarrollo')),
    locked_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(cycle_id, team_id),
    FOREIGN KEY (cycle_id) REFERENCES cycles(id) ON DELETE CASCADE,
    FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS economic_settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    effective_from TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    base_cost INTEGER NOT NULL,
    cost_per_member INTEGER NOT NULL,
    second_project_surcharge INTEGER NOT NULL,
    contract_price INTEGER NOT NULL,
    required_interventor_signatures INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS portfolios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    skills TEXT,
    tools TEXT,
    work_style TEXT,
    service_category TEXT NOT NULL DEFAULT 'programacion_robotica' CHECK(service_category IN ('programacion_robotica', 'pagina_web_simple', 'landing_html', 'automatizacion', 'otro')),
    status TEXT NOT NULL CHECK(status IN ('draft', 'published', 'archived')) DEFAULT 'draft',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS contracts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id INTEGER,
    robotics_team_id INTEGER NOT NULL,
    development_team_id INTEGER NOT NULL,
    client_team_id INTEGER,
    provider_team_id INTEGER,
    client_team_type TEXT,
    provider_team_type TEXT,
    provider_service_track TEXT,
    web_request_kind TEXT CHECK(web_request_kind IN ('create', 'modify')),
    target_team_site_id INTEGER,
    portfolio_id INTEGER,
    requested_amount INTEGER NOT NULL,
    reserved_amount INTEGER NOT NULL DEFAULT 0,
    payment_released INTEGER NOT NULL DEFAULT 0,
    contract_origin TEXT NOT NULL DEFAULT 'team_request' CHECK(contract_origin IN ('team_request', 'admin_offer')),
    service_category TEXT NOT NULL DEFAULT 'programacion_robotica' CHECK(service_category IN ('programacion_robotica', 'pagina_web_simple', 'landing_html', 'automatizacion', 'otro')),
    admin_offer_id INTEGER,
    status TEXT NOT NULL CHECK(status IN (
        'requested',
        'pending_interventor_activation',
        'active',
        'in_development',
        'submitted_for_review',
        'correction_required',
        'validated',
        'closed',
        'cancelled'
    )) DEFAULT 'requested',
    requested_by_user_id INTEGER,
    requested_delivery_date TEXT,
    request_message TEXT,
    request_file_path TEXT,
    request_original_filename TEXT,
    request_stored_filename TEXT,
    request_file_size INTEGER NOT NULL DEFAULT 0,
    paused_by_deadline INTEGER NOT NULL DEFAULT 0,
    paused_at TEXT,
    pause_reason TEXT,
    last_interventor_user_id INTEGER,
    last_interventor_comment TEXT,
    last_interventor_action TEXT,
    last_interventor_signed_at TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    activated_at TEXT,
    closed_at TEXT,
    FOREIGN KEY (cycle_id) REFERENCES cycles(id) ON DELETE SET NULL,
    FOREIGN KEY (robotics_team_id) REFERENCES teams(id) ON DELETE CASCADE,
    FOREIGN KEY (development_team_id) REFERENCES teams(id) ON DELETE CASCADE,
    FOREIGN KEY (target_team_site_id) REFERENCES team_sites(id) ON DELETE SET NULL,
    FOREIGN KEY (portfolio_id) REFERENCES portfolios(id) ON DELETE SET NULL,
    FOREIGN KEY (requested_by_user_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS admin_offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT,
    service_category TEXT NOT NULL DEFAULT 'programacion_robotica' CHECK(service_category IN ('programacion_robotica', 'pagina_web_simple', 'landing_html', 'automatizacion', 'otro')),
    reward_amount INTEGER NOT NULL DEFAULT 0,
    created_by_user_id INTEGER,
    cycle_id INTEGER,
    taken_by_team_id INTEGER,
    deadline TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open', 'taken', 'closed', 'cancelled')),
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE SET NULL,
    FOREIGN KEY (cycle_id) REFERENCES cycles(id) ON DELETE SET NULL,
    FOREIGN KEY (taken_by_team_id) REFERENCES teams(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS contract_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER NOT NULL,
    review_stage TEXT NOT NULL CHECK(review_stage IN ('contract_activation', 'final_delivery')),
    interventor_user_id INTEGER NOT NULL,
    decision TEXT NOT NULL CHECK(decision IN ('approved', 'rejected', 'correction_required')),
    comment TEXT NOT NULL,
    signed_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (contract_id) REFERENCES contracts(id) ON DELETE CASCADE,
    FOREIGN KEY (interventor_user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER NOT NULL,
    submitted_by_user_id INTEGER NOT NULL,
    delivery_notes TEXT,
    repository_link TEXT,
    code_text TEXT,
    file_path TEXT,
    original_filename TEXT,
    stored_filename TEXT,
    file_size INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK(status IN ('submitted', 'under_review', 'correction_required', 'validated')) DEFAULT 'submitted',
    submitted_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (contract_id) REFERENCES contracts(id) ON DELETE CASCADE,
    FOREIGN KEY (submitted_by_user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_wallet_id INTEGER,
    to_wallet_id INTEGER,
    amount INTEGER NOT NULL CHECK(amount >= 0),
    transaction_type TEXT NOT NULL CHECK(transaction_type IN (
        'issuance',
        'reward',
        'contract_payment',
        'maintenance',
        'adjustment',
        'penalty',
        'refund',
        'reserve'
    )),
    description TEXT,
    created_by_user_id INTEGER,
    cycle_id INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (from_wallet_id) REFERENCES wallets(id) ON DELETE SET NULL,
    FOREIGN KEY (to_wallet_id) REFERENCES wallets(id) ON DELETE SET NULL,
    FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE SET NULL,
    FOREIGN KEY (cycle_id) REFERENCES cycles(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS rewards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id INTEGER,
    robotics_team_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    amount INTEGER NOT NULL CHECK(amount >= 0),
    created_by_user_id INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (cycle_id) REFERENCES cycles(id) ON DELETE SET NULL,
    FOREIGN KEY (robotics_team_id) REFERENCES teams(id) ON DELETE CASCADE,
    FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS interventor_assignments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    student_id INTEGER,
    start_date TEXT DEFAULT CURRENT_TIMESTAMP,
    end_date TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS ai_assistant_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id INTEGER NOT NULL,
    asked_by_user_id INTEGER NOT NULL,
    question TEXT NOT NULL,
    pasted_code TEXT,
    source_kind TEXT NOT NULL CHECK(source_kind IN ('pasted_code', 'latest_delivery_code', 'latest_delivery_file')),
    source_excerpt TEXT,
    response_text TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('answered', 'blocked', 'error')) DEFAULT 'answered',
    model_name TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (contract_id) REFERENCES contracts(id) ON DELETE CASCADE,
    FOREIGN KEY (asked_by_user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id INTEGER,
    details TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS cycle_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id INTEGER NOT NULL UNIQUE,
    executed_by_user_id INTEGER,
    executed_at TEXT DEFAULT CURRENT_TIMESTAMP,
    maintenance_total INTEGER NOT NULL DEFAULT 0,
    custom_total INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    FOREIGN KEY (cycle_id) REFERENCES cycles(id) ON DELETE CASCADE,
    FOREIGN KEY (executed_by_user_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS cycle_custom_charges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id INTEGER NOT NULL,
    team_id INTEGER NOT NULL,
    amount INTEGER NOT NULL CHECK(amount >= 0),
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'applied', 'cancelled')) DEFAULT 'pending',
    created_by_user_id INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    applied_transaction_id INTEGER,
    FOREIGN KEY (cycle_id) REFERENCES cycles(id) ON DELETE CASCADE,
    FOREIGN KEY (team_id) REFERENCES teams(id) ON DELETE CASCADE,
    FOREIGN KEY (created_by_user_id) REFERENCES users(id) ON DELETE SET NULL,
    FOREIGN KEY (applied_transaction_id) REFERENCES transactions(id) ON DELETE SET NULL
);
