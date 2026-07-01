import base64
import datetime
import json
from pathlib import Path
from typing import Callable

PG_HOST     = "bdd-T0XX0052.alias"
PG_PORT     = "5577"
PG_DB       = "supervisionpsc_db"
PG_USER_RPT = "rptpsc"
PG_PWD_RPT  = "rptpsc_xx"

# ---------------------------------------------------------------------------
# Utilitaires navigation JSON
# ---------------------------------------------------------------------------
def _nav(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def _pct(n, total):
    if not n or not total:
        return 0.0
    try:
        return round(float(n) / float(total) * 100, 2)
    except Exception:
        return 0.0


def _fmt_num(val, default="—"):
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        return f"{val:,}".replace(",", "&nbsp;")
    return default


def fetch_history() -> list[dict]:
    try:
        from sqlalchemy import create_engine, text
        engine = create_engine(
            f"postgresql+psycopg://{PG_USER_RPT}:{PG_PWD_RPT}@{PG_HOST}:{PG_PORT}/{PG_DB}"
        )
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT flux_id, date_import, payload "
                "FROM rptpsc.output_kpi_json "
                "ORDER BY date_import ASC"
            )).fetchall()
        return [{"flux_id": r[0], "date_import": r[1], "payload": r[2]} for r in rows]
    except Exception:
        return []


def _parse_payload(p):
    if isinstance(p, str):
        try:
            return json.loads(p)
        except Exception:
            return {}
    return p if isinstance(p, dict) else {}


# ---------------------------------------------------------------------------
# Extraction KPI depuis payload
# ---------------------------------------------------------------------------
KPI_DEF = {
    "vol_total":         (["2_Volumetrie_Brute","Resultats","Total_Lignes"],                                   "Volume total",            "lignes",  False),
    "vol_assures":       (["2_Volumetrie_Brute","Resultats","Nbr_Assures_Brut"],                               "Assurés bruts",           "lignes",  False),
    "pop_unique":        (["3_Population_Unique","Global_Personnes"],                                          "Personnes uniques",       "pers.",   False),
    "cible_ciam":        (["3_Population_Unique","Cible_CIAM_Assures"],                                        "Cible CIAM",              "ass.",    False),
    "matching_ciam":     (["5_CIAM","Matching_Global","Global","Taux_Couverture"],                             "Taux matching CIAM",      "%",       True),
    "non_rap_ciam":      (["5_CIAM","Matching_Global","Global","Non_Rapproches"],                              "Non-rapprochés CIAM",     "pers.",   False),
    "score_qualite":     (["5_CIAM","Score_Qualite_Donnees","DATA_QUALITY_OK","Pct"],                          "Score qualité données",   "%",       True),
    "emails_risque":     (["5_CIAM","Score_Qualite_Donnees","Decomposition_KO","Email_CIAM_risque","Nombre"],  "Emails à risque",         "cptes",   False),
    "kpep_incoherent":   (["5_CIAM","Score_Qualite_Donnees","Decomposition_KO","KPEP_NS_diff_CIAM","Nombre"], "KPEP incohérents",        "pers.",   False),
    "ciam_sans_email":   (["5_CIAM","Annexe","Qualite_Comptes_CIAM","F1a_Sans_Email","Nombre"],                "CIAM sans email",         "cptes",   False),
    "ciam_sans_kpep":    (["5_CIAM","Annexe","Qualite_Comptes_CIAM","F1b_Sans_KPEP","Nombre"],                 "CIAM sans KPEP",          "cptes",   False),
    "doublons_email":    (["5_CIAM","Annexe","Qualite_Comptes_CIAM","F1d_Doublons_Email","Nb_Emails_Dupliques"],"Doublons email CIAM",    "emails",  False),
    "coherence_kpep":    (["5_CIAM","Annexe","Coherence_KPEP_3_Sources","E2_Coherence_3_Sources_Stricte","Pct"],"Cohérence KPEP 3 src",  "%",       True),
    "iehe_presents":     (["6_IEHE","Presence_Globale","Presents_IEHE","Nombre"],                              "Présents IEHE",           "pers.",   False),
    "iehe_manquants":    (["6_IEHE","Presence_Globale","Manquants_IEHE","Nombre"],                             "Manquants IEHE",          "pers.",   False),
    "iehe_taux":         (["6_IEHE","Presence_Globale","Presents_IEHE","Taux"],                                "Taux présence IEHE",      "%",       True),
    "tp_eligible":       (["7_Carte_TP","Eligibilite_Globale","Population_Eligible","Nombre"],                 "Éligibles TP",            "pers.",   False),
    "tp_futur":          (["7_Carte_TP","Eligibilite_Globale","Population_Future","Nombre"],                   "Future TP",               "pers.",   False),
    "tp_taux":           (["7_Carte_TP","Eligibilite_Globale","Population_Eligible","Taux"],                   "Taux éligibilité TP",     "%",       True),
    "ged_trouves":       (["7_Carte_TP","Controle_GED_Quotidien","Trouves_GED","Nombre"],                      "Cartes TP en GED",        "cartes",  False),
    "ged_taux":          (["7_Carte_TP","Controle_GED_Quotidien","Trouves_GED","Taux"],                        "Taux GED",                "%",       True),
    "ged_ko":            (["7_Carte_TP","Controle_GED_Quotidien","Non_Trouves_GED","Nombre"],                  "KO GED",                  "cartes",  False),
    "ddn_diff":          (["5_CIAM","Annexe","Incoherences_NS_CIAM","G1a_DDN_Differente","DDN_Differente","Nombre"],"DDN différentes",    "pers.",   False),
    "nom_divergent":     (["5_CIAM","Annexe","Incoherences_NS_CIAM","G1b_Nom_Prenom_Divergent","Nom_Divergent","Nombre"],"Noms divergents","pers.",   False),
    "prospects_ciam":    (["5_CIAM","Annexe","Prospects_CIAM","E1a_Comptes_CIAM_Prospects","Comptes_Prospects","Nombre"],"Prospects CIAM","cptes",   False),
}


def extract_kpi(payload: dict, key: str):
    path = KPI_DEF[key][0]
    cur = payload
    for k in path:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur if isinstance(cur, (int, float)) else None


def extract_all(payload: dict) -> dict:
    return {k: extract_kpi(payload, k) for k in KPI_DEF}


# ---------------------------------------------------------------------------
# Composants HTML
# ---------------------------------------------------------------------------
def _color(value, key: str, is_pct: bool) -> str:
    if value is None:
        return "#9ca3af"
    BAD = {"non_rap", "manquant", "risque", "incoher", "divergent", "sans_",
           "doublons", "futur", "ko", "ddn_diff", "nom_div", "prospect", "ciam_sans"}
    GOOD_PCT = {"matching", "score", "coherence", "taux", "iehe_taux", "tp_taux"}
    key_l = key.lower()
    if any(b in key_l for b in BAD):
        return "#6ab023" if value == 0 else ("#f59e0b" if value <= 10 else "#ef4444")
    if is_pct or any(g in key_l for g in GOOD_PCT):
        return "#6ab023" if value >= 99 else ("#f59e0b" if value >= 90 else "#ef4444")
    return "#3b82f6"


def _delta_html(current, previous, is_pct: bool, bad_key: bool) -> str:
    if current is None or previous is None:
        return '<span style="color:#9ca3af;font-size:11px">—</span>'
    delta = round(current - previous, 2)
    if delta == 0:
        return '<span style="color:#9ca3af;font-size:11px">= préc.</span>'
    arrow = "▲" if delta > 0 else "▼"
    # Pour les métriques "mauvaises" (non-rapprochés etc.) : hausse = rouge
    if bad_key:
        color = "#ef4444" if delta > 0 else "#6ab023"
    else:
        color = "#6ab023" if delta > 0 else "#ef4444"
    sign = "+" if delta > 0 else ""
    suffix = "%" if is_pct else ""
    return f'<span style="color:{color};font-size:11px">{arrow} {sign}{delta}{suffix} vs préc.</span>'


def _kpi_card(key: str, value, prev_value, label: str, unit: str, is_pct: bool) -> str:
    BAD_KEYS = {"non_rap_ciam","emails_risque","kpep_incoherent","ciam_sans_email",
                "ciam_sans_kpep","doublons_email","iehe_manquants","tp_futur",
                "ged_ko","ddn_diff","nom_divergent","prospects_ciam"}
    is_bad = key in BAD_KEYS
    col = _color(value, key, is_pct)
    val_str = f"{value:,}".replace(",", " ") if isinstance(value, int) else (f"{value:.2f}" if isinstance(value, float) else "—")
    delta_html = _delta_html(value, prev_value, is_pct, is_bad)
    return f"""
    <div class="kpi-card">
      <div class="kpi-label">{label}</div>
      <div class="kpi-value" style="color:{col}">{val_str}<span class="kpi-unit">&nbsp;{unit}</span></div>
      <div class="kpi-delta">{delta_html}</div>
    </div>"""


def _gauge(pct: float, label: str) -> str:
    r = 40
    circ = 2 * 3.14159 * r
    offset = circ * (1 - min(pct, 100) / 100)
    color = "#6ab023" if pct >= 99 else ("#f59e0b" if pct >= 90 else "#ef4444")
    return f"""
    <div class="gauge-wrap">
      <svg width="100" height="100" viewBox="0 0 100 100">
        <circle cx="50" cy="50" r="{r}" fill="none" stroke="#e5e7eb" stroke-width="8"/>
        <circle cx="50" cy="50" r="{r}" fill="none" stroke="{color}" stroke-width="8"
                stroke-dasharray="{circ:.1f}" stroke-dashoffset="{offset:.1f}"
                transform="rotate(-90 50 50)" stroke-linecap="round"/>
        <text x="50" y="46" text-anchor="middle" font-size="15" font-weight="700" fill="{color}">{pct:.1f}</text>
        <text x="50" y="60" text-anchor="middle" font-size="9" fill="#6b7280">%</text>
      </svg>
      <div class="gauge-label">{label}</div>
    </div>"""


def _chart(cid: str, ctype: str, labels: list, datasets: list, title: str = "",
           y_suffix: str = "", stacked: bool = False, height: int = 80) -> str:
    ds_js = json.dumps(datasets, ensure_ascii=False)
    lbl_js = json.dumps(labels, ensure_ascii=False)
    suffix_cb = f"(v) => v + '{y_suffix}'" if y_suffix else "(v) => v"
    stacked_js = "true" if stacked else "false"
    title_js = json.dumps(title)
    return f"""
    <div class="chart-wrap">
      {"<h4 class='chart-title'>" + title + "</h4>" if title else ""}
      <canvas id="{cid}" height="{height}"></canvas>
    </div>
    <script>
    (function(){{
      var ctx = document.getElementById('{cid}').getContext('2d');
      new Chart(ctx, {{
        type: '{ctype}',
        data: {{ labels: {lbl_js}, datasets: {ds_js} }},
        options: {{
          responsive: true,
          plugins: {{
            legend: {{ position: 'bottom', labels: {{ font: {{ size: 11 }} }} }},
            title: {{ display: false }}
          }},
          scales: {{
            x: {{ stacked: {stacked_js}, ticks: {{ font: {{ size: 10 }} }} }},
            y: {{ stacked: {stacked_js}, ticks: {{ callback: {suffix_cb}, font: {{ size: 10 }} }} }}
          }}
        }}
      }});
    }})();
    </script>"""


def _table(rows_data: list[list], headers: list[str], col_align: list[str] = None) -> str:
    if not rows_data:
        return '<p style="color:#9ca3af;font-style:italic">Aucune donnée</p>'
    align = col_align or ["left"] * len(headers)
    th = "".join(f'<th style="text-align:{a}">{h}</th>' for h, a in zip(headers, align))
    body = ""
    for row in rows_data:
        tds = "".join(f'<td style="text-align:{a}">{v}</td>'
                      for v, a in zip(row, align))
        body += f"<tr>{tds}</tr>"
    return f'<div class="table-wrap"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


# ---------------------------------------------------------------------------
# Aplatissement intégral du JSON (onglet « Toutes les données »)
# ---------------------------------------------------------------------------
def _flatten(obj, prefix=""):
    """Aplatit récursivement un dict/list imbriqué en lignes (chemin, valeur).
    Les feuilles (scalaires) sont émises ; les listes de scalaires sont jointes."""
    rows = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix} › {k}" if prefix else str(k)
            if isinstance(v, (dict, list)):
                rows.extend(_flatten(v, key))
            else:
                rows.append((key, v))
    elif isinstance(obj, list):
        # Liste de scalaires -> une ligne ; liste d'objets -> récursion indexée.
        if all(not isinstance(x, (dict, list)) for x in obj):
            rows.append((prefix, ", ".join("" if x is None else str(x) for x in obj)))
        else:
            for i, x in enumerate(obj):
                rows.extend(_flatten(x, f"{prefix} [{i}]"))
    else:
        rows.append((prefix, obj))
    return rows


_tid = [0]


def _copyable_table(title: str, rows: list[tuple]) -> str:
    """Tableau HTML copiable (bouton TSV → presse-papiers, idéal Excel)."""
    if not rows:
        return ""
    _tid[0] += 1
    tid = f"tbl{_tid[0]}"
    body = "".join(
        f'<tr><td style="text-align:left">{_esc(k)}</td>'
        f'<td style="text-align:right">{_esc(v)}</td></tr>'
        for k, v in rows
    )
    return f"""
    <div class="section-header" style="margin-top:24px;display:flex;align-items:center">
      <h3 style="margin:0">{_esc(title)}</h3>
      <button class="copy-btn" onclick="copyTable('{tid}')">📋 Copier (Excel)</button>
    </div>
    <div class="table-wrap"><table id="{tid}"><thead><tr>
      <th style="text-align:left">Indicateur</th><th style="text-align:right">Valeur</th>
    </tr></thead><tbody>{body}</tbody></table></div>"""


def _esc(v) -> str:
    s = "" if v is None else str(v)
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _build_all_data_tab(payload: dict) -> str:
    """Construit l'onglet listant TOUS les KPI du JSON, par section, en
    tableaux copiables."""
    if not isinstance(payload, dict) or not payload:
        return '<p style="color:#9ca3af">Aucune donnée.</p>'
    blocks = []
    total = 0
    for section, content in payload.items():
        rows = _flatten(content)
        total += len(rows)
        blocks.append(_copyable_table(f"{section}  ({len(rows)} indicateurs)", rows))
    intro = (f'<div style="padding:0 24px"><p style="color:#6b7280;font-size:13px;margin-bottom:8px">'
             f'Intégralité des indicateurs du flux — <b>{total}</b> valeurs. '
             f'Bouton « Copier (Excel) » : colle directement dans un classeur.</p>'
             f'<input type="text" id="alldata-search" placeholder="🔎 Rechercher un indicateur…" '
             f'oninput="filterAllData(this.value)" '
             f'style="width:100%;max-width:420px;padding:8px 12px;border:1px solid #d1d5db;'
             f'border-radius:8px;font-size:13px;margin-bottom:12px">'
             f'<div id="alldata-empty" style="display:none;color:#9ca3af;font-style:italic">Aucun résultat.</div>')
    return intro + "".join(blocks) + "</div>"


def _color_val(v, is_bad=False):
    if v is None or v == "—":
        return v
    try:
        clean_v = "".join(c for c in str(v) if c.isdigit() or c in ".-")
        fv = float(clean_v)
    except Exception:
        return v
    if is_bad:
        col = "#6ab023" if fv == 0 else ("#f59e0b" if fv <= 10 else "#ef4444")
    else:
        col = "#6ab023" if fv >= 99 else ("#f59e0b" if fv >= 90 else "#374151")
    return f'<span style="color:{col};font-weight:600">{v}</span>'


# ---------------------------------------------------------------------------
# Génération principale
# ---------------------------------------------------------------------------
_cid = [0]
def _next(p="c"):
    _cid[0] += 1
    return f"{p}{_cid[0]}"


def generate(
    json_path: Path,
    output_dir: Path,
    assets_dir: Path,
    progress_callback: Callable[[int, str], None] | None = None,
) -> Path:
    def prog(pct, msg=""):
        if progress_callback:
            progress_callback(pct, msg)

    prog(5, "Chargement JSON")
    payload = json.loads(json_path.read_text(encoding="utf-8"))

    prog(15, "Chargement historique BDD")
    history = fetch_history()
    hist_payloads = [_parse_payload(r["payload"]) for r in history]
    hist_dates = []
    for r in history:
        d = r["date_import"]
        hist_dates.append(d.strftime("%d/%m/%y") if hasattr(d, "strftime") else str(d))

    prog(25, "Extraction KPI")
    today_kpi = extract_all(payload)
    prev_kpi  = extract_all(hist_payloads[-1]) if len(hist_payloads) >= 1 else {}
    hist_kpi  = {k: [extract_kpi(p, k) for p in hist_payloads] for k in KPI_DEF}

    logo_path = assets_dir / "MGEN-logo.jpg"
    logo_b64  = base64.b64encode(logo_path.read_bytes()).decode() if logo_path.exists() else ""
    logo_tag  = f'<img src="data:image/jpeg;base64,{logo_b64}" height="52" alt="MGEN">' if logo_b64 else ""

    date_flux = json_path.stem.split("_")[0] if "_" in json_path.stem else json_path.stem
    now = datetime.datetime.now()

    # ── Métadonnées ────────────────────────────────────────────────────────
    meta = _nav(payload, "Metadata") or {}
    nb_flux = len(history)

    # ── Cartes KPI (top) ────────────────────────────────────────────────────
    def card(key):
        label, unit, is_pct = KPI_DEF[key][1:]
        return _kpi_card(key, today_kpi.get(key), prev_kpi.get(key), label, unit, is_pct)

    cards_vol = "".join(card(k) for k in ["vol_total","vol_assures","pop_unique","cible_ciam"])
    cards_ciam = "".join(card(k) for k in ["matching_ciam","non_rap_ciam","score_qualite",
                                             "coherence_kpep","ciam_sans_email","doublons_email",
                                             "kpep_incoherent","emails_risque"])
    cards_iehe = "".join(card(k) for k in ["iehe_taux","iehe_manquants"])
    cards_tp   = "".join(card(k) for k in ["tp_taux","tp_eligible","tp_futur","ged_taux","ged_trouves","ged_ko"])

    # ── Jauges de synthèse ─────────────────────────────────────────────────
    gauges = "".join(_gauge(today_kpi.get(k) or 0, KPI_DEF[k][1]) for k in
                     ["matching_ciam","iehe_taux","tp_taux","score_qualite","coherence_kpep","ged_taux"])

    # ── Onglet 1 : Vue d'ensemble ───────────────────────────────────────────
    vol = _nav(payload, "2_Volumetrie_Brute", "Resultats") or {}
    pop = _nav(payload, "3_Population_Unique") or {}

    tab1_charts = ""
    if hist_dates:
        tab1_charts += _chart(_next(), "line", hist_dates, [
            {"label":"Volume total","data":hist_kpi["vol_total"],"borderColor":"#3b82f6",
             "backgroundColor":"#3b82f620","tension":0.3,"fill":True,"spanGaps":True},
            {"label":"Assurés bruts","data":hist_kpi["vol_assures"],"borderColor":"#6ab023",
             "tension":0.3,"fill":False,"spanGaps":True},
        ], "Évolution du volume flux", "")

    vol_rows = [
        ["Total lignes", _fmt_num(vol.get('Total_Lignes')),
         _fmt_num(pop.get('Global_Personnes')), "Personnes uniques"],
        ["Assurés bruts", _fmt_num(vol.get('Nbr_Assures_Brut')),
         _fmt_num(pop.get('Cible_CIAM_Assures')), "Cible CIAM assurés"],
        ["Conjoints bruts", _fmt_num(vol.get('Nbr_Conjoints_Brut')),
         _fmt_num(pop.get('Conjoints_Uniques')), "Conjoints uniques"],
    ]
    tab1_table = _table(vol_rows, ["Indicateur", "Valeur (lignes)", "Valeur (pers. uniques)", "Indicateur"],
                        ["left","right","right","left"])

    qual = _nav(payload,"4_Qualite_Donnees","Indicateurs") or {}
    qual_rows = [
        ["Assurés en double lignes", str(qual.get("Assures_En_Double_Lignes","—"))],
        ["Conjoints en double lignes", str(qual.get("Conjoints_En_Double_Lignes","—"))],
        ["Personnes avec plusieurs KPEP", str(qual.get("Personnes_Plusieurs_KPEP","—"))],
        ["Doublons contrats même offre", str(qual.get("Doublons_Contrats_Meme_Offre","—"))],
        ["Doublons contrats offres différentes", str(qual.get("Doublons_Contrats_Diff_Offre","—"))],
        ["Assurés ET conjoints même ID", str(qual.get("Assure_ET_Conjoint_Meme_ID","—"))],
    ]
    tab1_qual = _table(qual_rows, ["Indicateur qualité source","Valeur"], ["left","right"])

    tab1 = f"""
    <div class="section-header"><h3>Volumes du flux</h3></div>
    <div class="kpi-grid">{cards_vol}</div>
    {tab1_charts}
    {tab1_table}
    <div class="section-header" style="margin-top:32px"><h3>Qualité données source</h3></div>
    {tab1_qual}"""

    # ── Onglet 2 : CIAM ────────────────────────────────────────────────────
    match_g = _nav(payload,"5_CIAM","Matching_Global","Global") or {}
    match_seg = _nav(payload,"5_CIAM","Annexe","Matching_Par_Segment","B2_Par_Societe","Resultats") or {}
    method_d  = _nav(payload,"5_CIAM","Annexe","Detail_Matching","Par_Methode") or {}
    source_d  = _nav(payload,"5_CIAM","Annexe","Detail_Matching","Par_Source_Fichier") or {}
    inc       = _nav(payload,"5_CIAM","Annexe","Incoherences_NS_CIAM") or {}
    score     = _nav(payload,"5_CIAM","Score_Qualite_Donnees") or {}
    cpts      = _nav(payload,"5_CIAM","Annexe","Qualite_Comptes_CIAM") or {}
    emails_q  = _nav(payload,"5_CIAM","Coherence_Emails","Email_CIAM_Rapproche_Egal_Val_Coord","Coherence_Emails") or {}
    prospects = _nav(payload,"5_CIAM","Annexe","Prospects_CIAM") or {}

    ciam_match_rows = [
        ["Cible assurés", str(match_g.get("Cible","—")), "100%"],
        ["Rapprochés",    _color_val(str(match_g.get("Rapproches","—"))),
                          _color_val(f'{match_g.get("Taux_Couverture","—")} %')],
        ["Non-rapprochés",_color_val(str(match_g.get("Non_Rapproches","—")), is_bad=True),
                          _color_val(f'{round(100 - (match_g.get("Taux_Couverture") or 100),2)} %', is_bad=True)],
    ]
    tab2_match = _table(ciam_match_rows, ["Catégorie","Nombre","Taux"], ["left","right","right"])

    method_rows = [[m, _fmt_num(n), f"{_pct(n, match_g.get('Rapproches',1)):.2f}%"]
                   for m, n in method_d.items()]
    tab2_method = _table(method_rows, ["Méthode de matching","Rapprochés","% rapprochés"],
                         ["left","right","right"])

    soc_rows = []
    for soc, v in match_seg.items():
        soc_rows.append([soc, str(v.get("Total","—")),
                         _color_val(str(v.get("Rapproches","—"))),
                         _color_val(str(v.get("Non_Rapproches","—")), is_bad=True),
                         _color_val(f'{v.get("Taux","—")} %')])
    tab2_soc = _table(soc_rows, ["Société","Total","Rapprochés","Non-rapprochés","Taux"],
                      ["left","right","right","right","right"])

    inc_rows = [
        ["DDN différente", str(_nav(inc,"G1a_DDN_Differente","DDN_Differente","Nombre") or "—"),
                           str(_nav(inc,"G1a_DDN_Differente","DDN_Differente","Pct") or "—")+"&nbsp;%",
                           str(_nav(inc,"G1a_DDN_Differente","Base") or "—")],
        ["Nom/Prénom divergent", str(_nav(inc,"G1b_Nom_Prenom_Divergent","Nom_Divergent","Nombre") or "—"),
                                  str(_nav(inc,"G1b_Nom_Prenom_Divergent","Nom_Divergent","Pct") or "—")+"&nbsp;%",
                                  str(_nav(inc,"G1b_Nom_Prenom_Divergent","Base") or "—")],
        ["Email coord ≠ email CIAM", str(_nav(inc,"G1c_Email_Coord_NS_Different_CIAM","Email_Different","Nombre") or "—"),
                                      str(_nav(inc,"G1c_Email_Coord_NS_Different_CIAM","Email_Different","Pct") or "—")+"&nbsp;%",
                                      str(_nav(inc,"G1c_Email_Coord_NS_Different_CIAM","Base") or "—")],
    ]
    tab2_inc = _table(inc_rows, ["Incohérence","Nombre","Taux","Base"], ["left","right","right","right"])

    cpts_rows = [
        ["Sans email",         str(_nav(cpts,"F1a_Sans_Email","Nombre") or "—"),
                               str(_nav(cpts,"F1a_Sans_Email","Pct") or "—")+"&nbsp;%"],
        ["Sans KPEP",          str(_nav(cpts,"F1b_Sans_KPEP","Nombre") or "—"),
                               str(_nav(cpts,"F1b_Sans_KPEP","Pct") or "—")+"&nbsp;%"],
        ["Email_other seulement", str(_nav(cpts,"F1c_Email_Other_Seulement","Nombre") or "—"),
                                  str(_nav(cpts,"F1c_Email_Other_Seulement","Pct") or "—")+"&nbsp;%"],
        ["Emails dupliqués",   str(_nav(cpts,"F1d_Doublons_Email","Nb_Emails_Dupliques") or "—"),
                               f'{_nav(cpts,"F1d_Doublons_Email","Nb_Comptes_Concernes") or "—"}&nbsp;cptes concernés'],
    ]
    tab2_cpts = _table(cpts_rows, ["Indicateur qualité compte","Valeur","Détail"], ["left","right","left"])

    score_rows = [
        ["DATA_QUALITY_OK", _color_val(str(_nav(score,"DATA_QUALITY_OK","Nombre") or "—")),
                            _color_val(str(_nav(score,"DATA_QUALITY_OK","Pct") or "—")+"&nbsp;%")],
        ["DATA_QUALITY_KO", _color_val(str(_nav(score,"DATA_QUALITY_KO","Nombre") or "—"), is_bad=True),
                            _color_val(str(_nav(score,"DATA_QUALITY_KO","Pct") or "—")+"&nbsp;%", is_bad=True)],
        ["— Email à risque",  str(_nav(score,"Decomposition_KO","Email_CIAM_risque","Nombre") or "—"), ""],
        ["— KPEP NS≠CIAM",    str(_nav(score,"Decomposition_KO","KPEP_NS_diff_CIAM","Nombre") or "—"), ""],
        ["— Prospect CIAM",   str(_nav(score,"Decomposition_KO","Prospect_CIAM","Nombre") or "—"), ""],
    ]
    tab2_score = _table(score_rows, ["Catégorie","Nombre","Taux"], ["left","right","right"])

    email_rows = [
        ["Emails identiques (vrais)", str(_nav(emails_q,"Vrais_Emails_Identiques","Nombre") or "—"),
                                       str(_nav(emails_q,"Vrais_Emails_Identiques","Pct") or "—")+"&nbsp;%"],
        ["Emails différents",          str(_nav(emails_q,"Mails_CIAM_Diff_Val_Coord","Nombre") or "—"),
                                       str(_nav(emails_q,"Mails_CIAM_Diff_Val_Coord","Pct") or "—")+"&nbsp;%"],
        ["CIAM présent / Coord absent",str(_nav(emails_q,"Detail_Diff","CIAM_Present_Coord_Absente","Nombre") or "—"), ""],
        ["Coord présent / CIAM absent",str(_nav(emails_q,"Detail_Diff","Coord_Presente_CIAM_Absent","Nombre") or "—"), ""],
        ["Deux emails distincts",      str(_nav(emails_q,"Detail_Diff","Deux_Emails_Distincts","Nombre") or "—"), ""],
        ["Email CIAM vide",            str(_nav(emails_q,"Mails_CIAM_Vides","Nombre") or "—"),
                                       str(_nav(emails_q,"Mails_CIAM_Vides","Pct") or "—")+"&nbsp;%"],
        ["Valeur coord vide",          str(_nav(emails_q,"Mails_Val_Coord_Vides","Nombre") or "—"),
                                       str(_nav(emails_q,"Mails_Val_Coord_Vides","Pct") or "—")+"&nbsp;%"],
    ]
    tab2_email = _table(email_rows, ["Cohérence email CIAM vs NS","Nombre","Taux"], ["left","right","right"])

    ns_top = _nav(payload,"5_CIAM","Qualite_Emails_Enrichie","NS_Emails_MailCIAM","Top_Domaines") or {}
    ck_top = _nav(payload,"5_CIAM","Annexe","Qualite_Emails_Enrichie","CIAM_Emails_Keycloak","Top_Domaines") or {}
    domains_rows = [[d, _fmt_num(n), _fmt_num(ck_top.get(d))]
                    for d, n in list(ns_top.items())[:10]]
    tab2_domains = _table(domains_rows, ["Domaine","NS (mailciam)","CIAM (Keycloak)"], ["left","right","right"])

    ciam_charts = ""
    if hist_dates:
        ciam_charts += _chart(_next(), "line", hist_dates, [
            {"label":"Taux matching (%)","data":hist_kpi["matching_ciam"],"borderColor":"#6ab023",
             "backgroundColor":"#6ab02320","tension":0.3,"fill":True,"spanGaps":True,"yAxisID":"y"},
            {"label":"Non-rapprochés","data":hist_kpi["non_rap_ciam"],"borderColor":"#ef4444",
             "tension":0.3,"fill":False,"spanGaps":True,"yAxisID":"y1"},
        ], "Tendance matching CIAM")
        ciam_charts += _chart(_next(), "line", hist_dates, [
            {"label":"Score qualité (%)","data":hist_kpi["score_qualite"],"borderColor":"#6ab023",
             "backgroundColor":"#6ab02320","tension":0.3,"fill":True,"spanGaps":True},
            {"label":"Emails à risque","data":hist_kpi["emails_risque"],"borderColor":"#f59e0b",
             "tension":0.3,"fill":False,"spanGaps":True},
            {"label":"KPEP incohérents","data":hist_kpi["kpep_incoherent"],"borderColor":"#ef4444",
             "tension":0.3,"fill":False,"spanGaps":True},
        ], "Tendance qualité données CIAM")
        ciam_charts += _chart(_next(), "line", hist_dates, [
            {"label":"Doublons email","data":hist_kpi["doublons_email"],"borderColor":"#ef4444",
             "tension":0.3,"spanGaps":True},
            {"label":"CIAM sans email","data":hist_kpi["ciam_sans_email"],"borderColor":"#f59e0b",
             "tension":0.3,"spanGaps":True},
        ], "Qualité comptes CIAM (historique)")

    tab2 = f"""
    <div class="kpi-grid">{cards_ciam}</div>
    <div class="section-header"><h3>Rapprochement global</h3></div>
    {tab2_match}
    <div class="section-header" style="margin-top:28px"><h3>Méthodes de matching</h3></div>
    {tab2_method}
    <div class="section-header" style="margin-top:28px"><h3>Par société</h3></div>
    {tab2_soc}
    <div class="section-header" style="margin-top:28px"><h3>Score qualité données</h3></div>
    {tab2_score}
    <div class="section-header" style="margin-top:28px"><h3>Incohérences NS ↔ CIAM</h3></div>
    {tab2_inc}
    <div class="section-header" style="margin-top:28px"><h3>Qualité comptes Keycloak</h3></div>
    {tab2_cpts}
    <div class="section-header" style="margin-top:28px"><h3>Cohérence emails</h3></div>
    {tab2_email}
    <div class="section-header" style="margin-top:28px"><h3>Top domaines email</h3></div>
    {tab2_domains}
    <div class="section-header" style="margin-top:28px"><h3>Tendances historiques</h3></div>
    {ciam_charts}"""

    # ── Onglet 3 : IEHE ────────────────────────────────────────────────────
    iehe_glob = _nav(payload,"6_IEHE","Presence_Globale") or {}
    iehe_qual = _nav(payload,"6_IEHE","Annexe","Qualite_Referentiel") or {}
    iehe_ecart = _nav(payload,"6_IEHE","Ecart_Lignes_vs_Personnes") or {}
    retry_val = _nav(payload,"6_IEHE","Annexe","Retry_IEHE_KO")

    iehe_pres_rows = [
        ["Présents IEHE",  _color_val(str(_nav(iehe_glob,"Presents_IEHE","Nombre") or "—")),
                           _color_val(str(_nav(iehe_glob,"Presents_IEHE","Taux") or "—")+"&nbsp;%")],
        ["Manquants IEHE", _color_val(str(_nav(iehe_glob,"Manquants_IEHE","Nombre") or "—"), is_bad=True),
                           _color_val(str(_nav(iehe_glob,"Manquants_IEHE","Taux") or "—")+"&nbsp;%", is_bad=True)],
        ["Dont assurés manquants", str(_nav(iehe_glob,"Manquants_IEHE","dont_Assures") or "—"), ""],
        ["Dont conjoints manquants", str(_nav(iehe_glob,"Manquants_IEHE","dont_Conjoints") or "—"), ""],
    ]
    tab3_pres = _table(iehe_pres_rows, ["Indicateur","Nombre","Taux"], ["left","right","right"])

    ecart_tot = _nav(iehe_ecart,"Total_Flux") or {}
    ecart_rows = [
        ["Total flux (lignes)", str(ecart_tot.get("Lignes","—"))],
        ["Personnes uniques",   str(ecart_tot.get("Personnes_Uniques","—"))],
        ["Delta doublons",      str(ecart_tot.get("Delta_Doublons","—"))],
    ]
    tab3_ecart = _table(ecart_rows, ["Indicateur","Valeur"], ["left","right"])

    d1 = _nav(iehe_qual,"D1_Completude_Email_IEHE") or {}
    d2 = _nav(iehe_qual,"D2_Concordance_Email_IEHE_CIAM") or {}
    d3 = _nav(iehe_qual,"D3_Concordance_Societe_IEHE_NS") or {}
    enrich = _nav(iehe_qual,"J1b_Potentiel_Enrichissement_CIAM") or {}

    iehe_qual_rows = [
        ["Email IEHE rempli",    str(_nav(d1,"Email_Rempli","Nombre") or "—"),
                                  str(_nav(d1,"Email_Rempli","Pct") or "—")+"&nbsp;%"],
        ["Email IEHE vide",      str(_nav(d1,"Email_Vide","Nombre") or "—"),
                                  str(_nav(d1,"Email_Vide","Pct") or "—")+"&nbsp;%"],
        ["Emails IEHE = CIAM",   str(_nav(d2,"Emails_Identiques","Nombre") or "—"),
                                  str(_nav(d2,"Emails_Identiques","Pct") or "—")+"&nbsp;%"],
        ["Emails IEHE ≠ CIAM",   str(_nav(d2,"Emails_Differents","Nombre") or "—"),
                                  str(_nav(d2,"Emails_Differents","Pct") or "—")+"&nbsp;%"],
        ["Potentiel enrichissement CIAM", str(_nav(enrich,"Potentiel_Enrichissement","Nombre") or "—"),
                                           str(_nav(enrich,"Potentiel_Enrichissement","Pct") or "—")+"&nbsp;%"],
        ["Sociétés IEHE = NS",   str(_nav(d3,"Societes_Identiques","Nombre") or "—"),
                                  str(_nav(d3,"Societes_Identiques","Pct") or "—")+"&nbsp;%"],
        ["Sociétés IEHE ≠ NS",   str(_nav(d3,"Societes_Differentes","Nombre") or "—"),
                                  str(_nav(d3,"Societes_Differentes","Pct") or "—")+"&nbsp;%"],
    ]
    tab3_qual = _table(iehe_qual_rows, ["Indicateur qualité IEHE","Valeur","Taux"], ["left","right","right"])

    kpep3 = _nav(payload,"5_CIAM","Annexe","Coherence_KPEP_3_Sources") or {}
    kpep3_rows = [
        ["NS = CIAM",          _color_val(str(_nav(kpep3,"E2_NS_Egal_CIAM","Nombre") or "—")),
                               _color_val(str(_nav(kpep3,"E2_NS_Egal_CIAM","Pct") or "—")+"&nbsp;%")],
        ["NS ≠ CIAM",          _color_val(str(_nav(kpep3,"E2_NS_Different_CIAM","Nombre") or "—"), is_bad=True),
                               _color_val(str(_nav(kpep3,"E2_NS_Different_CIAM","Pct") or "—")+"&nbsp;%", is_bad=True)],
        ["IEHE = CIAM",        _color_val(str(_nav(kpep3,"E2_IEHE_Egal_CIAM","Nombre") or "—")),
                               _color_val(str(_nav(kpep3,"E2_IEHE_Egal_CIAM","Pct") or "—")+"&nbsp;%")],
        ["IEHE ≠ CIAM",        _color_val(str(_nav(kpep3,"E2_IEHE_Different_CIAM","Nombre") or "—"), is_bad=True),
                               _color_val(str(_nav(kpep3,"E2_IEHE_Different_CIAM","Pct") or "—")+"&nbsp;%", is_bad=True)],
        ["Cohérence 3 sources",_color_val(str(_nav(kpep3,"E2_Coherence_3_Sources_Stricte","Nombre") or "—")),
                               _color_val(str(_nav(kpep3,"E2_Coherence_3_Sources_Stricte","Pct") or "—")+"&nbsp;%")],
    ]
    tab3_kpep = _table(kpep3_rows, ["KPEP 3 sources (NS / CIAM / IEHE)","Nombre","Taux"], ["left","right","right"])

    iehe_charts = ""
    if hist_dates:
        iehe_charts += _chart(_next(), "line", hist_dates, [
            {"label":"Taux présence IEHE (%)","data":hist_kpi["iehe_taux"],"borderColor":"#3b82f6",
             "backgroundColor":"#3b82f620","tension":0.3,"fill":True,"spanGaps":True},
            {"label":"Manquants IEHE","data":hist_kpi["iehe_manquants"],"borderColor":"#ef4444",
             "tension":0.3,"fill":False,"spanGaps":True},
        ], "Tendance présence IEHE")
        iehe_charts += _chart(_next(), "line", hist_dates, [
            {"label":"Cohérence KPEP 3 sources (%)","data":hist_kpi["coherence_kpep"],"borderColor":"#6ab023",
             "backgroundColor":"#6ab02320","tension":0.3,"fill":True,"spanGaps":True},
        ], "Cohérence KPEP historique")

    retry_html = ""
    if isinstance(retry_val, str):
        retry_html = f'<p style="color:#9ca3af;font-style:italic">{retry_val}</p>'
    elif isinstance(retry_val, dict):
        retry_rows = [[k, str(v)] for k, v in retry_val.items() if not isinstance(v, dict)]
        retry_html = _table(retry_rows, ["Indicateur","Valeur"], ["left","right"])

    tab3 = f"""
    <div class="kpi-grid">{cards_iehe}</div>
    <div class="section-header"><h3>Présence globale IEHE</h3></div>
    {tab3_pres}
    <div class="section-header" style="margin-top:28px"><h3>Lignes vs personnes uniques</h3></div>
    {tab3_ecart}
    <div class="section-header" style="margin-top:28px"><h3>Qualité référentiel IEHE</h3></div>
    {tab3_qual}
    <div class="section-header" style="margin-top:28px"><h3>Cohérence KPEP 3 sources</h3></div>
    {tab3_kpep}
    <div class="section-header" style="margin-top:28px"><h3>Retry IEHE KO</h3></div>
    {retry_html}
    <div class="section-header" style="margin-top:28px"><h3>Tendances historiques</h3></div>
    {iehe_charts}"""

    # ── Onglet 4 : Carte TP ─────────────────────────────────────────────────
    tp_glob  = _nav(payload,"7_Carte_TP","Eligibilite_Globale") or {}
    tp_types = _nav(payload,"7_Carte_TP","Detail_Par_Type","Detail_Par_Type") or {}
    tp_enr   = _nav(payload,"7_Carte_TP","TP_Enrichi_Operationnel") or {}
    ged      = _nav(payload,"7_Carte_TP","Controle_GED_Quotidien") or {}
    ged_soc  = _nav(ged,"Par_Societe") or {}

    tp_mois = _nav(tp_glob,"Eligible_Par_Mois") or {}
    tp_mois_rows = [[m, str(n), f"{_pct(n, tp_glob.get('Population_Eligible',{}).get('Nombre',1)):.1f}%"]
                    for m, n in tp_mois.items()]
    tab4_mois = _table(tp_mois_rows, ["Mois adhésion","Éligibles","% du total"], ["left","right","right"])

    tp_type_rows = [[k, str(v.get("cartes_tp","—")), str(v.get("future_tp","—"))]
                    for k, v in tp_types.items()]
    tab4_types = _table(tp_type_rows, ["Société | Offre | Type","Éligibles TP","Future TP"],
                        ["left","right","right"])

    c1 = _nav(tp_enr,"C1_Eligibles_TP_Non_Rapproches") or {}
    c2 = _nav(tp_enr,"C2_Delai_Futur_TP") or {}
    c3 = _nav(tp_enr,"C3_Repartition_TP_Par_Societe","Resultats") or {}
    tp_enr_rows = [
        ["Éligibles TP non rapprochés CIAM", str(_nav(c1,"Eligibles_TP_Non_Rapproches","Nombre") or "—"),
                                              str(_nav(c1,"Eligibles_TP_Non_Rapproches","Pct_Sur_Eligibles") or "—")+"&nbsp;%"],
        ["Future TP — délai moyen (jours)",  str(c2.get("Delai_Moyen_Jours","—")), ""],
        ["Future TP — délai médian (jours)", str(c2.get("Delai_Median_Jours","—")), ""],
        ["Future TP — délai max (jours)",    str(c2.get("Delai_Max_Jours","—")), ""],
    ]
    tab4_enr = _table(tp_enr_rows, ["Indicateur opérationnel","Valeur","Taux"], ["left","right","right"])

    ged_soc_rows = [[soc, str(v.get("Population_Eligible","—")),
                     str(v.get("Trouves_GED","—")),
                     _color_val(f'{v.get("Taux_GED","—")}&nbsp;%', is_bad=False),
                     _color_val(str(v.get("Rapproche_Final","—")))]
                    for soc, v in ged_soc.items()]
    tab4_ged_soc = _table(ged_soc_rows, ["Société","Éligibles","Trouvés GED","Taux GED","Rapprochés final"],
                           ["left","right","right","right","right"])

    tp_charts = ""
    if hist_dates:
        tp_charts += _chart(_next(), "line", hist_dates, [
            {"label":"Taux éligibilité TP (%)","data":hist_kpi["tp_taux"],"borderColor":"#6ab023",
             "backgroundColor":"#6ab02320","tension":0.3,"fill":True,"spanGaps":True},
            {"label":"Future TP","data":hist_kpi["tp_futur"],"borderColor":"#f59e0b",
             "tension":0.3,"fill":False,"spanGaps":True},
        ], "Tendance éligibilité TP")
        tp_charts += _chart(_next(), "line", hist_dates, [
            {"label":"Taux GED (%)","data":hist_kpi["ged_taux"],"borderColor":"#3b82f6",
             "backgroundColor":"#3b82f620","tension":0.3,"fill":True,"spanGaps":True},
            {"label":"KO GED","data":hist_kpi["ged_ko"],"borderColor":"#ef4444",
             "tension":0.3,"fill":False,"spanGaps":True},
        ], "Tendance Contrôle GED")

    tp_bar_labels = list(tp_mois.keys())
    tp_bar_data   = list(tp_mois.values())
    if tp_bar_labels:
        tp_charts = _chart(_next(), "bar", tp_bar_labels, [
            {"label":"Éligibles TP","data":tp_bar_data,"backgroundColor":"#6ab023aa",
             "borderColor":"#6ab023","borderWidth":1},
        ], "Répartition éligibles TP par mois d'adhésion") + tp_charts

    tab4 = f"""
    <div class="kpi-grid">{cards_tp}</div>
    <div class="section-header"><h3>Éligibilité globale</h3></div>
    {tab4_mois}
    <div class="section-header" style="margin-top:28px"><h3>Détail par société / offre / type</h3></div>
    {tab4_types}
    <div class="section-header" style="margin-top:28px"><h3>Indicateurs opérationnels</h3></div>
    {tab4_enr}
    <div class="section-header" style="margin-top:28px"><h3>Contrôle GED par société</h3></div>
    {tab4_ged_soc}
    <div class="section-header" style="margin-top:28px"><h3>Tendances historiques</h3></div>
    {tp_charts}"""

    # ── Onglet 5 : Historique ──────────────────────────────────────────────
    hist_charts = ""
    if hist_dates:
        hist_charts += _chart(_next(), "line", hist_dates, [
            {"label":"Matching CIAM (%)","data":hist_kpi["matching_ciam"],"borderColor":"#6ab023","tension":0.3,"spanGaps":True},
            {"label":"Présence IEHE (%)","data":hist_kpi["iehe_taux"],"borderColor":"#3b82f6","tension":0.3,"spanGaps":True},
            {"label":"Éligibilité TP (%)","data":hist_kpi["tp_taux"],"borderColor":"#f59e0b","tension":0.3,"spanGaps":True},
            {"label":"Score qualité (%)","data":hist_kpi["score_qualite"],"borderColor":"#a855f7","tension":0.3,"spanGaps":True},
            {"label":"Cohérence KPEP (%)","data":hist_kpi["coherence_kpep"],"borderColor":"#06b6d4","tension":0.3,"spanGaps":True},
        ], "Tous KPI principaux (%) — historique complet", height=120)
        hist_charts += _chart(_next(), "line", hist_dates, [
            {"label":"Volume total","data":hist_kpi["vol_total"],"borderColor":"#3b82f6","tension":0.3,"spanGaps":True},
            {"label":"Éligibles TP","data":hist_kpi["tp_eligible"],"borderColor":"#6ab023","tension":0.3,"spanGaps":True},
        ], "Évolution volumes", height=80)

    # Tableau récapitulatif flux
    hist_table_rows = []
    for i, (row, d) in enumerate(zip(history, hist_dates)):
        p = _parse_payload(row["payload"])
        vals = [extract_kpi(p, k) for k in KPI_DEF]
        def fmt(v, key):
            if v is None:
                return "—"
            is_pct = KPI_DEF[key][3]
            s = f"{v:.1f}%" if is_pct else (f"{v:,}".replace(",","&nbsp;") if isinstance(v,int) else str(v))
            return _color_val(s, is_bad=key in {"non_rap_ciam","emails_risque","iehe_manquants","ged_ko","kpep_incoherent"})
        row_vals = [str(row["flux_id"]), d] + [fmt(extract_kpi(p, k), k) for k in
                    ["vol_total","matching_ciam","non_rap_ciam","score_qualite",
                     "iehe_taux","tp_taux","ged_taux","ged_ko"]]
        hist_table_rows.append(row_vals)

    hist_table = _table(hist_table_rows,
        ["Flux","Date","Volume","Matching CIAM","Non-rap.","Qualité","IEHE","Elig. TP","GED %","GED KO"],
        ["left","left","right","right","right","right","right","right","right","right"])

    tab5 = f"""
    {hist_charts}
    <div class="section-header" style="margin-top:28px"><h3>Récapitulatif {nb_flux} flux en base</h3></div>
    {hist_table}"""

    # ── Onglet « Toutes les données » : tous les KPI du JSON, copiables ──────
    tab_all = _build_all_data_tab(payload)

    prog(75, "Assemblage HTML")

    # ── HTML final ──────────────────────────────────────────────────────────
    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="UTF-8">
  <title>Dashboard Supervision PSC — {date_flux}</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: "Segoe UI", Arial, sans-serif; background: #f4f6f9; color: #1a1a2e; font-size: 14px; }}
    /* Header */
    .rpt-header {{
      display: flex; align-items: center; gap: 20px;
      background: linear-gradient(135deg, #2d5a1b 0%, #4d8019 100%);
      padding: 20px 32px; color: #fff;
    }}
    .rpt-header h1 {{ font-size: 20px; font-weight: 700; }}
    .rpt-header p  {{ font-size: 12px; opacity: .8; margin-top: 3px; }}
    .rpt-meta {{ display: flex; gap: 24px; margin-top: 6px; flex-wrap: wrap; }}
    .rpt-meta span {{ font-size: 12px; background: rgba(255,255,255,.15);
                      border-radius: 12px; padding: 2px 10px; }}
    /* Navigation */
    .tab-nav {{ display: flex; gap: 2px; background: #fff; border-bottom: 2px solid #e5e7eb;
                padding: 0 24px; position: sticky; top: 0; z-index: 100;
                box-shadow: 0 2px 4px rgba(0,0,0,.06); }}
    .tab-btn {{ padding: 12px 18px; cursor: pointer; border: none; background: none;
                font-size: 13px; font-weight: 600; color: #6b7280; border-bottom: 3px solid transparent;
                margin-bottom: -2px; transition: all .2s; white-space: nowrap; }}
    .tab-btn:hover {{ color: #4d8019; }}
    .tab-btn.active {{ color: #4d8019; border-bottom-color: #6ab023; }}
    /* Synthèse */
    .synth-top {{ background: #fff; border-radius: 12px; padding: 24px 32px;
                  box-shadow: 0 1px 4px rgba(0,0,0,.08); margin: 24px; }}
    /* Jauges */
    .gauge-row {{ display: flex; flex-wrap: wrap; gap: 16px; justify-content: space-around;
                  margin: 20px 0; }}
    .gauge-wrap {{ text-align: center; }}
    .gauge-label {{ font-size: 11px; color: #6b7280; margin-top: 4px; font-weight: 600; }}
    /* Cartes KPI */
    .kpi-grid {{ display: flex; flex-wrap: wrap; gap: 12px; margin: 16px 0; }}
    .kpi-card {{
      background: #fff; border-radius: 10px; padding: 16px 18px; min-width: 160px; flex: 1;
      box-shadow: 0 1px 3px rgba(0,0,0,.07); border-left: 4px solid #6ab023;
    }}
    .kpi-label {{ font-size: 11px; color: #6b7280; font-weight: 600; text-transform: uppercase;
                  letter-spacing: .4px; margin-bottom: 6px; }}
    .kpi-value {{ font-size: 24px; font-weight: 800; line-height: 1.1; }}
    .kpi-unit  {{ font-size: 12px; font-weight: 400; color: #9ca3af; }}
    .kpi-delta {{ font-size: 11px; margin-top: 6px; }}
    /* Sections */
    .tab-content {{ display: none; padding: 0 24px 40px; }}
    .tab-content.active {{ display: block; }}
    .section-header {{ margin: 28px 0 10px; }}
    .section-header h3 {{ font-size: 15px; font-weight: 700; color: #4d8019;
                           border-left: 4px solid #6ab023; padding-left: 10px; }}
    /* Tables */
    .table-wrap {{ overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; background: #fff;
             border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.07); }}
    thead tr {{ background: #f0f7e6; }}
    th {{ padding: 9px 12px; font-weight: 700; color: #374151; border-bottom: 2px solid #e5e7eb;
          font-size: 12px; text-transform: uppercase; letter-spacing: .3px; }}
    td {{ padding: 8px 12px; border-bottom: 1px solid #f3f4f6; }}
    tr:last-child td {{ border-bottom: none; }}
    tr:hover {{ background: #fafff5; }}
    /* Charts */
    .chart-wrap {{ background: #fff; border-radius: 10px; padding: 20px;
                   box-shadow: 0 1px 3px rgba(0,0,0,.07); margin: 16px 0; }}
    .chart-title {{ font-size: 13px; font-weight: 700; color: #374151; margin-bottom: 12px; }}
    /* Bouton copier (onglet Toutes les données) */
    .copy-btn {{ margin-left: auto; font-size: 11px; font-weight: 600; cursor: pointer;
                 background: #6ab023; color: #fff; border: none; border-radius: 6px;
                 padding: 4px 10px; }}
    .copy-btn:hover {{ background: #4d8019; }}
    /* Alertes */
    .alert {{ border-radius: 8px; padding: 10px 16px; margin: 8px 0; font-size: 13px; }}
    .alert-ok   {{ background: #f0fdf4; border-left: 4px solid #6ab023; color: #166534; }}
    .alert-warn {{ background: #fffbeb; border-left: 4px solid #f59e0b; color: #92400e; }}
    .alert-err  {{ background: #fef2f2; border-left: 4px solid #ef4444; color: #991b1b; }}
  </style>
</head>
<body>

<header class="rpt-header">
  {logo_tag}
  <div>
    <h1>Dashboard Supervision PSC — MGEN</h1>
    <p>Flux du {date_flux} &nbsp;·&nbsp; Généré le {now.strftime('%d/%m/%Y à %H:%M')}</p>
    <div class="rpt-meta">
      <span>📊 {nb_flux} flux en base</span>
      <span>👤 {today_kpi.get('pop_unique','—')} personnes uniques</span>
      <span>🎯 {today_kpi.get('matching_ciam','—')} % matching CIAM</span>
      <span>💳 {today_kpi.get('tp_eligible','—')} éligibles TP</span>
      <span>🏥 {today_kpi.get('ged_taux','—')} % GED</span>
    </div>
  </div>
</header>

<nav class="tab-nav">
  <button class="tab-btn active" onclick="showTab('synthese',this)">📋 Synthèse</button>
  <button class="tab-btn" onclick="showTab('volumes',this)">📦 Volumes</button>
  <button class="tab-btn" onclick="showTab('ciam',this)">🔑 CIAM</button>
  <button class="tab-btn" onclick="showTab('iehe',this)">🏥 IEHE</button>
  <button class="tab-btn" onclick="showTab('tp',this)">💳 Carte TP</button>
  <button class="tab-btn" onclick="showTab('historique',this)">📈 Historique</button>
  <button class="tab-btn" onclick="showTab('alldata',this)">📋 Toutes les données</button>
</nav>

<!-- SYNTHÈSE -->
<div id="tab-synthese" class="tab-content active" style="padding-top:24px">
  <div class="synth-top">
    <div style="font-size:13px;font-weight:700;color:#374151;margin-bottom:12px">Indicateurs clés du flux</div>
    <div class="gauge-row">{gauges}</div>
  </div>
  <div style="padding:0 24px">
    <div class="section-header"><h3>Alertes du flux</h3></div>
    {_alerts_html(today_kpi)}
    <div class="section-header" style="margin-top:28px"><h3>KPI principaux</h3></div>
    {_summary_table(today_kpi, prev_kpi)}
  </div>
</div>

<!-- VOLUMES -->
<div id="tab-volumes" class="tab-content">{tab1}</div>

<!-- CIAM -->
<div id="tab-ciam" class="tab-content">{tab2}</div>

<!-- IEHE -->
<div id="tab-iehe" class="tab-content">{tab3}</div>

<!-- CARTE TP -->
<div id="tab-tp" class="tab-content">{tab4}</div>

<!-- HISTORIQUE -->
<div id="tab-historique" class="tab-content">{tab5}</div>

<!-- TOUTES LES DONNÉES -->
<div id="tab-alldata" class="tab-content">{tab_all}</div>

<script>
function showTab(name, btn) {{
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  btn.classList.add('active');
}}
function copyTable(id) {{
  var t = document.getElementById(id);
  if (!t) return;
  var lines = [];
  t.querySelectorAll('tr').forEach(function(tr) {{
    var cells = [];
    tr.querySelectorAll('th,td').forEach(function(c) {{ cells.push(c.innerText); }});
    lines.push(cells.join('\\t'));
  }});
  var tsv = lines.join('\\n');
  navigator.clipboard.writeText(tsv).then(function() {{
    var b = event.target; var old = b.textContent;
    b.textContent = '✓ Copié'; setTimeout(function() {{ b.textContent = old; }}, 1500);
  }});
}}
function filterAllData(q) {{
  q = (q || '').toLowerCase();
  var tab = document.getElementById('tab-alldata');
  if (!tab) return;
  var anyVisible = false;
  tab.querySelectorAll('table').forEach(function(t) {{
    var tableHasMatch = false;
    t.querySelectorAll('tbody tr').forEach(function(tr) {{
      var match = tr.innerText.toLowerCase().indexOf(q) !== -1;
      tr.style.display = match ? '' : 'none';
      if (match) {{ tableHasMatch = true; anyVisible = true; }}
    }});
    // Masque le bloc (titre + table) si aucune ligne ne correspond.
    var wrap = t.closest('.table-wrap');
    var header = wrap ? wrap.previousElementSibling : null;
    if (wrap) wrap.style.display = tableHasMatch ? '' : 'none';
    if (header && header.classList.contains('section-header'))
      header.style.display = tableHasMatch ? '' : 'none';
  }});
  var empty = document.getElementById('alldata-empty');
  if (empty) empty.style.display = (q && !anyVisible) ? 'block' : 'none';
}}
</script>
</body>
</html>"""

    prog(90, "Écriture fichier")
    out_path = output_dir / f"rapport_codir_{date_flux}.html"
    out_path.write_text(html, encoding="utf-8")

    try:
        from weasyprint import HTML as WHTML
        WHTML(string=html).write_pdf(str(output_dir / f"rapport_codir_{date_flux}.pdf"))
    except Exception:
        pass

    prog(100, "Rapport généré")
    return out_path


def _alerts_html(kpi: dict) -> str:
    alerts = []
    matching = kpi.get("matching_ciam")
    if matching is not None:
        if matching >= 99.5:
            alerts.append(("ok",  f"Matching CIAM : {matching}% — excellent"))
        elif matching >= 97:
            alerts.append(("warn", f"Matching CIAM : {matching}% — en dessous de 99.5%"))
        else:
            alerts.append(("err",  f"Matching CIAM : {matching}% — critique (seuil 97%)"))

    iehe = kpi.get("iehe_taux")
    if iehe is not None:
        if iehe >= 99.9:
            alerts.append(("ok",  f"Présence IEHE : {iehe}% — complète"))
        elif iehe >= 99:
            alerts.append(("warn", f"Présence IEHE : {iehe}% — quelques manquants"))
        else:
            alerts.append(("err",  f"Présence IEHE : {iehe}% — manquants significatifs"))

    ged = kpi.get("ged_taux")
    if ged is not None:
        if ged >= 50:
            alerts.append(("ok",  f"Contrôle GED : {ged}% — taux normal"))
        elif ged >= 10:
            alerts.append(("warn", f"Contrôle GED : {ged}% — taux bas"))
        else:
            alerts.append(("err",  f"Contrôle GED : {ged}% — très bas, vérifier la GED"))

    score = kpi.get("score_qualite")
    if score is not None:
        if score >= 99:
            alerts.append(("ok",  f"Qualité données : {score}%"))
        elif score >= 97:
            alerts.append(("warn", f"Qualité données : {score}% — anomalies détectées"))
        else:
            alerts.append(("err",  f"Qualité données : {score}% — qualité dégradée"))

    nr = kpi.get("non_rap_ciam", 0)
    if nr:
        alerts.append(("warn" if nr <= 20 else "err",
                        f"{nr} personnes non rapprochées dans CIAM — action manuelle possible"))

    if not alerts:
        alerts.append(("ok", "Aucune alerte — tous les indicateurs sont dans les normes"))

    lines = [f'<div class="alert alert-{lvl}">{msg}</div>' for lvl, msg in alerts]
    return "\n".join(lines)


def _summary_table(today: dict, prev: dict) -> str:
    KEYS = [
        ("vol_total",     False),("matching_ciam",  True),("non_rap_ciam",   False),
        ("score_qualite", True), ("coherence_kpep", True),("iehe_taux",       True),
        ("iehe_manquants",False),("tp_taux",         True),("tp_eligible",    False),
        ("tp_futur",      False),("ged_taux",         True),("ged_ko",         False),
        ("doublons_email",False),("ciam_sans_email",  False),("ddn_diff",      False),
        ("nom_divergent", False),("prospects_ciam",   False),
    ]
    rows = []
    BAD_KEYS = {"non_rap_ciam","iehe_manquants","tp_futur","ged_ko","doublons_email",
                "ciam_sans_email","ddn_diff","nom_divergent","prospects_ciam","emails_risque","kpep_incoherent"}
    for key, is_pct in KEYS:
        label, unit, _ = KPI_DEF[key][1:]
        v = today.get(key)
        p = prev.get(key)
        if v is None:
            continue
        v_str = f"{v:.2f} %" if is_pct else (f"{int(v):,}".replace(",","&nbsp;")+" "+unit if isinstance(v,(int,float)) else str(v))
        is_bad = key in BAD_KEYS
        p_str = "—"
        delta_str = "—"
        if p is not None:
            p_str = f"{p:.2f} %" if is_pct else (f"{int(p):,}".replace(",","&nbsp;") if isinstance(p,(int,float)) else str(p))
            delta = round(v - p, 2)
            sign = "+" if delta > 0 else ""
            suffix = "%" if is_pct else ""
            if delta == 0:
                delta_str = "="
            else:
                arrow = "▲" if delta > 0 else "▼"
                good = (not is_bad and delta > 0) or (is_bad and delta < 0)
                col = "#6ab023" if good else "#ef4444"
                delta_str = f'<span style="color:{col}">{arrow} {sign}{delta}{suffix}</span>'
        rows.append([label, _color_val(v_str, is_bad=is_bad), p_str, delta_str])
    return _table(rows, ["Indicateur","Flux du jour","Flux précédent","Δ"],
                  ["left","right","right","center"])
