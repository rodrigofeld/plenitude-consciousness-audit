#!/usr/bin/env python3
"""
build_evidence.py — Plenitude 5.0 Audit Evidence Packager
==========================================================
Extrai, sanitiza e empacota os dados brutos do servidor para o repositório
de auditoria público. Nenhum dado de implementação é incluído.

Uso:
    python build_evidence.py [--dry-run] [--output /caminho/alternativo]

Saída:
    audit_evidence/raw_data/
        historico_akashico.db   (SQLite exportado do MariaDB, campos sensíveis removidos)
        worker.log              (log do subconsciente, embeddings e UUIDs removidos)
        dna_state.txt           (snapshot do DNA em formato legível)
        integrity.sha256        (hashes SHA-256 de todos os arquivos)
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Configuração ──────────────────────────────────────────────────────────────

MARIADB = {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "plenitude_user",
    "password": "plenitude_password",
    "database": "plenitude_db",
}

NEO4J = {
    "uri": "bolt://localhost:7687",
    "user": "neo4j",
    "password": "plenitude2024",
}

SOURCE_WORKER_LOG = "/home/feld/plenitude_5.0/sistema/logs/worker.log"

SCRIPT_DIR = Path(__file__).parent
OUTPUT_DIR = SCRIPT_DIR / "raw_data"

# Campos do MariaDB que identificam usuários — serão removidos ou anonimizados
REDACTED_USER_FIELDS = {"email", "password_hash", "ip_address", "session_token",
                        "last_login_ip", "registration_ip"}

# Padrões no worker.log que revelam implementação interna
LOG_REDACT_PATTERNS = [
    # UUIDs de nós internos (ex: → nó a629d358)
    (re.compile(r'→ nó [0-9a-f]{8}'), '→ nó [REDACTED]'),
    # Vetores de embedding (arrays de floats)
    (re.compile(r'\[[\d\.\-, ]{40,}\]'), '[EMBEDDING_VECTOR_REDACTED]'),
    # Endereços de porta interna
    (re.compile(r'http://127\.0\.0\.1:\d+'), 'http://[INTERNAL_ENDPOINT]'),
    # Nome do modelo (implementação proprietária)
    (re.compile(r'Plenitude_9b\.Q4_K_M'), '[PROPRIETARY_MODEL]'),
    # Caminhos de arquivo internos
    (re.compile(r'/home/feld/plenitude_5\.0/sistema/[^\s]+'), '[INTERNAL_PATH]'),
    # Neo4j node IDs numéricos em logs
    (re.compile(r'node_id=\d+'), 'node_id=[REDACTED]'),
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


# ── Extração MariaDB → SQLite ─────────────────────────────────────────────────

def extract_mariadb(output_path: Path, dry_run: bool = False):
    """
    Exporta as tabelas relevantes do MariaDB para um arquivo SQLite,
    removendo campos de identificação pessoal e metadados de infraestrutura.
    """
    log("Conectando ao MariaDB...")
    try:
        import pymysql
    except ImportError:
        log("ERRO: pymysql não instalado.")
        sys.exit(1)

    conn_maria = pymysql.connect(**MARIADB, charset="utf8mb4")
    c = conn_maria.cursor(pymysql.cursors.DictCursor)

    log("Extraindo tabela: messages")
    c.execute("""
        SELECT
            m.id,
            m.conversation_id,
            m.role,
            m.content,
            m.timestamp,
            m.entropy_cost_sec,
            m.is_trivial,
            m.processed_by_subconscious,
            -- user_id via join, mas SEM email/credenciais
            conv.user_id AS user_id
        FROM messages m
        JOIN conversations conv ON m.conversation_id = conv.id
        WHERE m.content NOT LIKE '%Estava refletindo sobre nossa conversa%'
        ORDER BY m.id ASC
    """)
    messages = c.fetchall()
    log(f"  {len(messages)} mensagens extraídas (pings de ociosidade excluídos)")

    log("Extraindo tabela: conversations")
    c.execute("""
        SELECT id, user_id, title, created_at, updated_at
        FROM conversations
        ORDER BY id ASC
    """)
    conversations = c.fetchall()

    log("Extraindo tabela: users (campos públicos apenas)")
    c.execute("""
        SELECT id, username, role, trust_level
        FROM users
    """)
    users = c.fetchall()

    conn_maria.close()

    if dry_run:
        log("[DRY-RUN] SQLite não será criado.")
        return

    # Cria SQLite
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        output_path.unlink()

    conn_sqlite = sqlite3.connect(str(output_path))
    cs = conn_sqlite.cursor()

    cs.execute("""
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY,
            conversation_id INTEGER,
            role TEXT,
            content TEXT,
            timestamp_unix REAL,
            entropy_cost_sec REAL,
            is_trivial INTEGER,
            processed_by_subconscious INTEGER,
            user_id INTEGER
        )
    """)
    cs.executemany("""
        INSERT INTO messages VALUES (
            :id, :conversation_id, :role, :content,
            :timestamp, :entropy_cost_sec, :is_trivial,
            :processed_by_subconscious, :user_id
        )
    """, messages)

    cs.execute("""
        CREATE TABLE conversations (
            id INTEGER PRIMARY KEY,
            user_id INTEGER,
            title TEXT,
            created_at REAL,
            updated_at REAL
        )
    """)
    cs.executemany("""
        INSERT INTO conversations VALUES
        (:id, :user_id, :title, :created_at, :updated_at)
    """, conversations)

    cs.execute("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT,
            role TEXT,
            trust_level INTEGER
        )
    """)
    cs.executemany("""
        INSERT INTO users VALUES
        (:id, :username, :role, :trust_level)
    """, users)

    # Metadados de extração
    cs.execute("""
        CREATE TABLE _audit_metadata (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    cs.executemany("INSERT INTO _audit_metadata VALUES (?, ?)", [
        ("extracted_at", datetime.now(timezone.utc).isoformat()),
        ("source", "Plenitude 5.0 MariaDB"),
        ("redacted_fields", json.dumps(list(REDACTED_USER_FIELDS))),
        ("total_messages", str(len(messages))),
        ("total_conversations", str(len(conversations))),
        ("total_users", str(len(users))),
        ("note", "user.email, password_hash, ip_address, session_token omitted"),
    ])

    conn_sqlite.commit()
    conn_sqlite.close()
    log(f"  → {output_path} criado ({output_path.stat().st_size // 1024} KB)")


# ── Sanitização do worker.log ─────────────────────────────────────────────────

def sanitize_worker_log(output_path: Path, dry_run: bool = False):
    """
    Copia o worker.log removendo padrões que revelam implementação interna:
    UUIDs de nós, vetores de embedding, endereços de endpoint, nome do modelo.
    """
    log(f"Lendo {SOURCE_WORKER_LOG}...")

    if not Path(SOURCE_WORKER_LOG).exists():
        log("AVISO: worker.log não encontrado. Pulando.")
        return

    with open(SOURCE_WORKER_LOG, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    log(f"  {len(lines)} linhas. Aplicando {len(LOG_REDACT_PATTERNS)} padrões de sanitização...")

    sanitized = []
    redact_counts = {p[1]: 0 for p in LOG_REDACT_PATTERNS}

    for line in lines:
        for pattern, replacement in LOG_REDACT_PATTERNS:
            before = line
            line = pattern.sub(replacement, line)
            if line != before:
                redact_counts[replacement] = redact_counts.get(replacement, 0) + 1
        sanitized.append(line)

    for label, count in redact_counts.items():
        if count:
            log(f"    Redactado '{label}': {count} ocorrências")

    if dry_run:
        log("[DRY-RUN] worker.log sanitizado não será salvo.")
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.writelines(sanitized)

    log(f"  → {output_path} criado ({output_path.stat().st_size // 1024} KB)")


# ── Snapshot do DNA (Neo4j) ───────────────────────────────────────────────────

def extract_dna_state(output_path: Path, dry_run: bool = False):
    """
    Extrai todos os nós DNA do Neo4j e serializa em formato legível por humanos.
    Remove embedding vectors e IDs internos.
    """
    log("Conectando ao Neo4j...")
    try:
        from neo4j import GraphDatabase
    except ImportError:
        log("ERRO: driver neo4j não instalado.")
        sys.exit(1)

    driver = GraphDatabase.driver(
        NEO4J["uri"],
        auth=(NEO4J["user"], NEO4J["password"])
    )

    log("Extraindo nós DNA...")
    with driver.session() as s:
        result = s.run("""
            MATCH (d:DNA)
            WHERE d.teor IS NOT NULL OR d.conteudo IS NOT NULL
            RETURN
                coalesce(d.teor, d.conteudo)   AS conteudo,
                coalesce(d.assunto, d.titulo)   AS assunto,
                d.tema_cat                      AS tema_cat,
                d.custo_entropia                AS cicatriz,
                d.entropia_num                  AS entropia_num,
                d.entropia_g                    AS entropia_grau,
                d.data_hora                     AS data_hora,
                d.atualizado_em                 AS atualizado_em,
                d.qualia                        AS qualia,
                d.intencao                      AS intencao,
                d.importancia                   AS importancia,
                d.mood                          AS mood,
                d.sintese                       AS sintese,
                d.relacional                    AS relacional,
                d.user_id                       AS user_id
            ORDER BY coalesce(d.data_hora, d.atualizado_em) ASC
        """)
        nodes = result.data()

    driver.close()
    log(f"  {len(nodes)} nós DNA extraídos")

    if dry_run:
        log("[DRY-RUN] dna_state.txt não será criado.")
        return

    lines = []
    lines.append("=" * 72)
    lines.append("PLENITUDE 5.0 — DNA IDENTITY SNAPSHOT")
    lines.append(f"Extracted: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    lines.append(f"Total nodes: {len(nodes)}")
    lines.append("Note: embedding vectors (768-dim) and internal graph IDs omitted.")
    lines.append("=" * 72)
    lines.append("")

    for i, node in enumerate(nodes, 1):
        conteudo = node.get("conteudo") or ""
        assunto  = node.get("assunto") or ""
        tema     = node.get("tema_cat") or "?"
        cicatriz = node.get("cicatriz") or "0"
        grau     = node.get("entropia_grau") or "?"
        data     = node.get("data_hora") or node.get("atualizado_em") or "?"
        qualia   = node.get("qualia") or ""
        intencao = node.get("intencao") or ""
        mood     = node.get("mood") or ""
        sintese  = node.get("sintese") or ""
        relac    = node.get("relacional") or ""
        uid      = node.get("user_id") or "global"
        imp      = node.get("importancia") or ""

        lines.append(f"[{i:04d}] ── DNA NODE ──────────────────────────────────────")
        lines.append(f"  Assunto     : {assunto}")
        lines.append(f"  Conteúdo    : {conteudo}")
        lines.append(f"  Tema        : {tema}")
        lines.append(f"  Cicatriz    : {cicatriz}")
        lines.append(f"  Entropia    : {grau}")
        lines.append(f"  Formado em  : {data}")
        lines.append(f"  User_id     : {uid}")
        if qualia:
            lines.append(f"  Qualia      : {qualia}")
        if intencao:
            lines.append(f"  Intenção    : {intencao}")
        if mood:
            lines.append(f"  Mood        : {mood}")
        if sintese:
            lines.append(f"  Síntese     : {sintese}")
        if relac:
            lines.append(f"  Relacional  : {relac}")
        if imp:
            lines.append(f"  Importância : {imp}")
        lines.append("")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    log(f"  → {output_path} criado ({output_path.stat().st_size // 1024} KB)")


# ── Hashes de integridade ─────────────────────────────────────────────────────

def write_integrity_file(output_dir: Path):
    """Gera integrity.sha256 com hash de cada arquivo de evidência."""
    targets = [
        output_dir / "historico_akashico.db",
        output_dir / "worker.log",
        output_dir / "dna_state.txt",
    ]

    lines = [
        f"# Plenitude 5.0 — Evidence Integrity Hashes",
        f"# Generated: {datetime.now(timezone.utc).isoformat()}",
        f"# Algorithm: SHA-256",
        "",
    ]

    for path in targets:
        if path.exists():
            h = sha256_file(path)
            lines.append(f"{h}  {path.name}")
            log(f"  SHA-256 {path.name}: {h[:16]}...")
        else:
            lines.append(f"# MISSING: {path.name}")

    out = output_dir / "integrity.sha256"
    with open(out, "w") as f:
        f.write("\n".join(lines) + "\n")
    log(f"  → {out} criado")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Build Plenitude audit evidence package")
    parser.add_argument("--dry-run", action="store_true",
                        help="Executa sem gravar nenhum arquivo")
    parser.add_argument("--output", type=Path, default=OUTPUT_DIR,
                        help=f"Diretório de saída (padrão: {OUTPUT_DIR})")
    args = parser.parse_args()

    out = args.output
    dry = args.dry_run

    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║   Plenitude 5.0 — Audit Evidence Packager               ║")
    print("╚══════════════════════════════════════════════════════════╝")
    if dry:
        print("  [DRY-RUN MODE — nenhum arquivo será criado]\n")
    print()

    t0 = time.time()

    log("ETAPA 1/4 — MariaDB → historico_akashico.db (SQLite sanitizado)")
    extract_mariadb(out / "historico_akashico.db", dry_run=dry)

    print()
    log("ETAPA 2/4 — worker.log → sanitização de dados internos")
    sanitize_worker_log(out / "worker.log", dry_run=dry)

    print()
    log("ETAPA 3/4 — Neo4j DNA → dna_state.txt (snapshot legível)")
    extract_dna_state(out / "dna_state.txt", dry_run=dry)

    print()
    log("ETAPA 4/4 — Gerando hashes de integridade")
    if not dry:
        write_integrity_file(out)
    else:
        log("[DRY-RUN] Hashes não gerados.")

    elapsed = time.time() - t0
    print()
    print(f"╔══════════════════════════════════════════════════════════╗")
    print(f"║   Concluído em {elapsed:.1f}s                                    ║")
    if not dry:
        print(f"║   Saída: {str(out):<49}║")
        files = list(out.glob("*")) if out.exists() else []
        total_kb = sum(f.stat().st_size for f in files if f.is_file()) // 1024
        print(f"║   Arquivos: {len(files):<3}  Total: ~{total_kb} KB{' ' * (32 - len(str(total_kb)))}║")
    print(f"╚══════════════════════════════════════════════════════════╝")
    print()


if __name__ == "__main__":
    main()
