"""Stage-1 source probe: can the Librarian get every source for every shortlist company?"""
import csv, json, re, sys, time, urllib.request, urllib.parse, datetime as dt
from collections import defaultdict

DATA = sys.argv[1]  # dir holding ob/products.txt and pb/pb.csv
UA_SEC = "NeeleyHackathon research admin@example.com"
UA_WEB = "Mozilla/5.0"

def get(url, ua=UA_WEB, tries=3):
    import subprocess
    for i in range(tries):
        p = subprocess.run(["curl", "-sfL", "--max-time", "40", "-A", ua, url], capture_output=True)
        if p.returncode == 0:
            return p.stdout
        time.sleep(1 + i)
    raise RuntimeError(f"curl {p.returncode} {url}")

def sec(url):
    time.sleep(0.15)
    return json.loads(get(url, UA_SEC))

SHORTLIST = {"Merck": 310158, "BMS": 14272, "Pfizer": 78003, "Eli Lilly": 59478,
             "Novo Nordisk": 353278, "AbbVie": 1551152, "J&J": 200406,
             "AstraZeneca": 901832, "Amgen": 318154, "Gilead": 882095}

# ---------- 1. SEC filings + XBRL ----------
def all_filings(cik):
    d = sec(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
    r = d["filings"]["recent"]
    rows = [dict(zip(r.keys(), vals)) for vals in zip(*r.values())]
    return d, rows

def probe_company(name, cik):
    d, rows = all_filings(cik)
    forms = {x["form"] for x in rows}
    foreign = "20-F" in forms and "10-K" not in forms
    annual_form = "20-F" if foreign else "10-K"
    annuals = [x for x in rows if x["form"] == annual_form][:2]
    out = {"company": name, "filer": "20-F/6-K" if foreign else "10-K/10-Q",
           "annuals": [f'{a["reportDate"]} ({a["filingDate"]})' for a in annuals]}
    if foreign:
        sixk = [x for x in rows if x["form"] == "6-K"]
        out["6k_count_since_2026"] = sum(x["filingDate"] >= "2026-01-01" for x in sixk)
        # heuristic: financial reports are the big ones
        big = [x for x in sixk if x["size"] > 1_000_000 and x["filingDate"] >= "2025-04-01"]
        out["big_6ks"] = [f'{x["filingDate"]} {x["primaryDocument"]} {x["size"]//1000}KB' for x in big][:8]
    else:
        qs = [x for x in rows if x["form"] == "10-Q"]
        latest = qs[0]
        ld = dt.date.fromisoformat(latest["reportDate"])
        prior = ld.replace(year=ld.year - 1).isoformat()
        out["latest_q"] = latest["reportDate"]
        out["prior_yr_q"] = any(x["reportDate"] == prior for x in qs)
    # XBRL
    try:
        f = sec(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json")["facts"]
        tax = [t for t in f if t != "dei"]
        out["taxonomy"] = tax
        units, q_recent = set(), False
        for t in tax:
            for concept, v in f[t].items():
                for u, facts in v["units"].items():
                    for x in facts:
                        if x["end"] >= "2025-01-01":
                            units.add(u)
                            if x["form"] in ("10-Q", "6-K") and x["end"] >= "2026-01-01":
                                q_recent = True
        out["currency"] = sorted(u for u in units if re.fullmatch(r"[A-Z]{3}", u))
        out["xbrl_quarterly_2026"] = q_recent
    except Exception as e:
        out["xbrl_error"] = str(e)
    return out

# ---------- 2. Rival makers on EDGAR ----------
RIVALS = ["ROCHE", "BOEHRINGER", "NOVARTIS", "SANOFI", "TAKEDA", "REGENERON",
          "BRIDGEBIO", "ALNYLAM", "GSK", "SHIONOGI", "VIIV"]

def probe_rivals():
    tick = json.loads(get("https://www.sec.gov/files/company_tickers.json", UA_SEC))
    res = {}
    for r in RIVALS:
        hits = [v for v in tick.values() if r in v["title"].upper()]
        if not hits:
            res[r] = "not an SEC registrant"
            continue
        h = hits[0]
        d, rows = all_filings(h["cik_str"])
        forms = [x["form"] for x in rows if x["filingDate"] >= "2025-01-01"]
        kinds = sorted({fm for fm in forms if fm in ("10-K", "10-Q", "20-F", "6-K", "40-F")})
        res[r] = f'{h["title"]} ({h["ticker"]}) files: {", ".join(kinds) or "no periodic reports"}'
    return res

# ---------- 3. Labels + Orange/Purple Book ----------
PRODUCTS = {  # brand -> case appendix maker
    "Keytruda": "Merck", "Januvia": "Merck", "Lipfendra": "Merck",
    "Opdivo": "BMS", "Eliquis": "BMS",
    "Ibrance": "Pfizer", "Vyndaqel": "Pfizer",
    "Mounjaro": "Lilly", "Zepbound": "Lilly", "Trulicity": "Lilly", "Verzenio": "Lilly", "Foundayo": "Lilly",
    "Ozempic": "Novo", "Wegovy": "Novo",
    "Skyrizi": "AbbVie", "Rinvoq": "AbbVie", "Humira": "AbbVie",
    "Stelara": "J&J", "Tremfya": "J&J", "Darzalex": "J&J", "Xarelto": "J&J", "Rybrevant": "J&J", "Lazcluze": "J&J",
    "Tagrisso": "AZ", "Farxiga": "AZ", "Imfinzi": "AZ",
    "Repatha": "Amgen", "Enbrel": "Amgen", "Amjevita": "Amgen", "Wezlana": "Amgen",
    "Biktarvy": "Gilead", "Yeztugo": "Gilead",
    "Tecentriq": "Roche", "Jardiance": "BI", "Kisqali": "Novartis", "Cosentyx": "Novartis", "Leqvio": "Novartis",
    "Attruby": "BridgeBio", "Amvuttra": "Alnylam", "Entyvio": "Takeda", "Sarclisa": "Sanofi",
    "Praluent": "Regeneron", "Dovato": "ViiV", "Cabenuva": "ViiV", "Apretude": "ViiV",
}

def load_ob():
    rows = defaultdict(set)
    with open(f"{DATA}/ob/products.txt", encoding="latin-1") as fh:
        for r in csv.DictReader(fh, delimiter="~"):
            rows[r["Trade_Name"].upper()].add((r["Appl_Type"] + r["Appl_No"], r["Applicant_Full_Name"]))
    return rows

def load_pb():
    rows = defaultdict(set)
    with open(f"{DATA}/pb/pb.csv", encoding="utf-8-sig", errors="ignore") as fh:
        lines = fh.read().splitlines()
    hdr = next(i for i, l in enumerate(lines) if l.startswith("N/R/U,Applicant") or l.startswith(",Applicant") or "BLA Number" in l)
    for r in csv.DictReader(lines[hdr:]):
        if r.get("Proprietary Name"):
            rows[r["Proprietary Name"].upper()].add(("BLA" + r["BLA Number"], r["Applicant"]))
    return rows

def probe_label(brand):
    q = urllib.parse.quote(brand)
    d = json.loads(get(f"https://dailymed.nlm.nih.gov/dailymed/services/v2/spls.json?drug_name={q}&pagesize=100"))
    spls = [x for x in d["data"] if brand.upper() in x["title"].upper()]
    labelers = defaultdict(list)
    for x in spls:
        m = re.search(r"\[([^\]]+)\]\s*$", x["title"])
        labelers[m.group(1) if m else "?"].append(x)
    codes = {}
    for lab, xs in labelers.items():
        x = xs[0]
        try:
            nd = json.loads(get(f'https://dailymed.nlm.nih.gov/dailymed/services/v2/spls/{x["setid"]}/ndcs.json'))
            codes[lab] = sorted({n["ndc"].split("-")[0] for n in nd["data"]["ndcs"]})
        except Exception:
            codes[lab] = ["?"]
    return {"spl_count": len(spls), "labelers": {k: (len(v), codes[k]) for k, v in labelers.items()}}

if __name__ == "__main__":
    result = {"companies": [], "rivals": {}, "products": {}}
    for n, c in SHORTLIST.items():
        try:
            result["companies"].append(probe_company(n, c))
        except Exception as e:
            result["companies"].append({"company": n, "error": str(e)})
        print("done", n, file=sys.stderr)
    result["rivals"] = probe_rivals()
    print("done rivals", file=sys.stderr)
    ob, pb = load_ob(), load_pb()
    for b, maker in PRODUCTS.items():
        try:
            lab = probe_label(b)
        except Exception as e:
            lab = {"error": str(e)}
        result["products"][b] = {"maker": maker, "orange_book": sorted(ob.get(b.upper(), [])),
                                 "purple_book": sorted(pb.get(b.upper(), [])), "dailymed": lab}
        print("done", b, file=sys.stderr)
    json.dump(result, sys.stdout, indent=1)
