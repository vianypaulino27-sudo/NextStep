"""Import U.S. Department of Education College Scorecard institution data.

Source: "Most Recent Institution-Level Data" from https://collegescorecard.ed.gov/data/
(U.S. government work, public domain). We keep a slim, documented subset of
columns for every currently operating, degree-granting institution
(CURROPER = 1 and HIGHDEG >= 2, i.e. awards at least an associate degree).

Usage:
  DATABASE_URL=... python3 scripts/import_scorecard.py path/to/Most-Recent-Cohorts-Institution.csv
  DATABASE_URL=... python3 scripts/import_scorecard.py --download     # fetches the zip named below

Re-running replaces rows by UNITID (upsert) and refreshes college_data_source.
Institutions that disappear from a newer file are left in place but can be
listed with --report-stale.

Coverage: only U.S. institutions that participate in federal student aid
(Title IV) and report to IPEDS. It does NOT cover every college worldwide.
"""
import argparse
import csv
import io
import json
import os
import re
import sys
import urllib.request
import zipfile

import psycopg
from psycopg.types.json import Jsonb

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from college_match import CIP, normalize  # noqa: E402  (single source of truth)

DATA_PAGE = "https://collegescorecard.ed.gov/data/"
ZIP_URL = "https://ed-public-download.scorecard.network/downloads/Most-Recent-Cohorts-Institution_06102026.zip"
RELEASED = "June 10, 2026 (College Scorecard data update)"

# From CollegeScorecardDataDictionary.xlsx, sheet Most_Recent_Inst_Cohort_Map (June 2026 release).
DATA_YEARS = {
    "name_location_programs": "Academic year 2025-26 (IPEDS); programs offered: AY2024-25, CIP 2020 codes",
    "admissions": "Fall 2024 admissions (IPEDS 2024-25): admission rate, SAT/ACT ranges",
    "enrollment": "Fall 2024 undergraduate enrollment (IPEDS 2024-25)",
    "tuition": "Academic year 2024-25 published tuition and fees (IPEDS 2024-25)",
    "cost_and_net_price": "Academic year 2023-24 cost of attendance and average net price (IPEDS 2024-25)",
    "pell_share": "Academic year 2023-24 (IPEDS 2024-25)",
    "completion": "Students who started in fall 2018 / 2018-19, completion within 150% of normal time (IPEDS 2024-25)",
    "retention": "Fall 2023 full-time first-time cohort (IPEDS 2024-25)",
    "median_debt": "Federal loan borrowers who completed in FY2020-FY2021 (NSLDS, pooled)",
    "median_earnings_10yr": "Students who entered in AY2009-10/2010-11, earnings measured 2020-21, in 2022 dollars",
}

INCOME_BANDS = [("1", "0-30000"), ("2", "30001-48000"), ("3", "48001-75000"), ("4", "75001-110000"), ("5", "110001+")]


def num(v, cast=float):
    if v in (None, "", "NA", "NULL", "PrivacySuppressed", "PS"):
        return None
    try:
        return cast(float(v)) if cast is int else cast(v)
    except ValueError:
        return None


def web(url):
    url = (url or "").strip()
    if not url or url in ("NA", "NULL"):
        return None
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    url = re.sub(r"^http://", "https://", url, flags=re.I)
    return url if re.match(r"^https://[A-Za-z0-9.-]+\.[A-Za-z]{2,}(/[^\s\"'<>]*)?$", url) else None


def row_to_record(r):
    control = num(r.get("CONTROL"), int)
    pub = control == 1
    net = num(r.get("NPT4_PUB") if pub else r.get("NPT4_PRIV"), int)
    bands = {}
    for i, label in INCOME_BANDS:
        v = num(r.get(f"NPT4{i}_PUB") if pub else r.get(f"NPT4{i}_PRIV"), int)
        if v is not None:
            bands[label] = v
    bach = [name for code, name in CIP.items() if (num(r.get(f"CIP{code}BACHL"), int) or 0) > 0]
    assoc = [name for code, name in CIP.items() if (num(r.get(f"CIP{code}ASSOC"), int) or 0) > 0]
    alias = r.get("ALIAS")
    alias = None if alias in (None, "", "NA", "NULL") else alias.strip()
    name = r["INSTNM"].strip()
    return {
        "unitid": int(r["UNITID"]),
        "opeid": r.get("OPEID") or None,
        "name": name,
        "alias": alias,
        "city": r.get("CITY") or None,
        "state": (r.get("STABBR") or "")[:2] or None,
        "zip": r.get("ZIP") or None,
        "website": web(r.get("INSTURL")),
        "net_price_calc_url": web(r.get("NPCURL")),
        "control": control,
        "pred_degree": num(r.get("PREDDEG"), int),
        "high_degree": num(r.get("HIGHDEG"), int),
        "locale": num(r.get("LOCALE"), int),
        "latitude": num(r.get("LATITUDE")),
        "longitude": num(r.get("LONGITUDE")),
        "undergrad_enrollment": num(r.get("UGDS"), int),
        "admission_rate": num(r.get("ADM_RATE")),
        "open_admission": {1: True, 2: False}.get(num(r.get("OPENADMP"), int)),
        "test_policy": num(r.get("ADMCON7"), int),
        "sat_avg": num(r.get("SAT_AVG"), int),
        "sat_rw_25": num(r.get("SATVR25"), int), "sat_rw_75": num(r.get("SATVR75"), int),
        "sat_math_25": num(r.get("SATMT25"), int), "sat_math_75": num(r.get("SATMT75"), int),
        "act_25": num(r.get("ACTCM25"), int), "act_75": num(r.get("ACTCM75"), int),
        "tuition_in_state": num(r.get("TUITIONFEE_IN"), int),
        "tuition_out_state": num(r.get("TUITIONFEE_OUT"), int),
        "cost_of_attendance": num(r.get("COSTT4_A"), int) or num(r.get("COSTT4_P"), int),
        "avg_net_price": net,
        "net_price_by_income": Jsonb(bands) if bands else None,
        "completion_rate": num(r.get("C150_4")) if num(r.get("C150_4")) is not None else num(r.get("C150_L4")),
        "retention_rate": num(r.get("RET_FT4")) if num(r.get("RET_FT4")) is not None else num(r.get("RET_FTL4")),
        "pell_share": num(r.get("PCTPELL")),
        "median_debt": num(r.get("GRAD_DEBT_MDN"), int),
        "median_earnings_10yr": num(r.get("MD_EARN_WNE_P10"), int),
        "bachelor_programs": bach,
        "associate_programs": assoc,
        "hbcu": num(r.get("HBCU"), int) == 1,
        "hsi": num(r.get("HSI"), int) == 1,
        "women_only": num(r.get("WOMENONLY"), int) == 1,
        "men_only": num(r.get("MENONLY"), int) == 1,
        "distance_only": num(r.get("DISTANCEONLY"), int) == 1,
        "search_text": normalize(" ".join(x for x in [name, alias or "", r.get("CITY") or ""] if x)),
    }


def keep(r):
    return r.get("CURROPER") == "1" and (num(r.get("HIGHDEG"), int) or 0) >= 2


def read_rows(fh):
    reader = csv.DictReader(fh)
    for r in reader:
        if keep(r):
            yield row_to_record(r)


def upsert(conn, records):
    records = list(records)
    if not records:
        return 0
    cols = list(records[0])
    upd = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c != "unitid")
    sql = (f"INSERT INTO college_scorecard ({', '.join(cols)}, imported_at) "
           f"VALUES ({', '.join(['%s'] * len(cols))}, CURRENT_TIMESTAMP) "
           f"ON CONFLICT (unitid) DO UPDATE SET {upd}, imported_at=CURRENT_TIMESTAMP")
    with conn.cursor() as cur:
        cur.executemany(sql, [[rec[c] for c in cols] for rec in records])  # pipelined by psycopg 3
    return len(records)


def record_source(conn, file_name, count):
    conn.execute(
        """INSERT INTO college_data_source (id, title, source_url, file_name, released, data_years, row_count, imported_at)
           VALUES ('scorecard', 'U.S. Department of Education College Scorecard', %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
           ON CONFLICT (id) DO UPDATE SET title=EXCLUDED.title, source_url=EXCLUDED.source_url,
             file_name=EXCLUDED.file_name, released=EXCLUDED.released, data_years=EXCLUDED.data_years,
             row_count=EXCLUDED.row_count, imported_at=EXCLUDED.imported_at""",
        (DATA_PAGE, file_name, RELEASED, Jsonb(DATA_YEARS), count),
    )


def open_csv(path, download):
    if download:
        print(f"downloading {ZIP_URL}")
        data = urllib.request.urlopen(ZIP_URL, timeout=300).read()
        zf = zipfile.ZipFile(io.BytesIO(data))
        name = next(n for n in zf.namelist() if n.endswith(".csv") and not n.startswith("__MACOSX"))
        return io.TextIOWrapper(zf.open(name), encoding="utf-8-sig", newline=""), os.path.basename(ZIP_URL)
    return open(path, encoding="utf-8-sig", newline=""), os.path.basename(path)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", nargs="?", help="Most-Recent-Cohorts-Institution.csv")
    ap.add_argument("--download", action="store_true")
    args = ap.parse_args(argv)
    if not args.csv and not args.download:
        ap.error("give a CSV path or --download")
    fh, fname = open_csv(args.csv, args.download)
    with fh, psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        n = upsert(conn, read_rows(fh))
        total = conn.execute("SELECT count(*) FROM college_scorecard").fetchone()[0]
        record_source(conn, fname if not args.download else os.path.basename(ZIP_URL), total)
        conn.commit()
    print(f"college_scorecard: upserted {n} rows; table now has {total}")


if __name__ == "__main__":
    sys.exit(main())
