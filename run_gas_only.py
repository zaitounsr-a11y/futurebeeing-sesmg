import os, sys, json, shutil, subprocess, csv
from pathlib import Path

ESMP_ROOT   = Path(r"C:\Users\zaito\esmp")
TEMPLATE    = ESMP_ROOT / "tests/scenarios/Waerme_Gasheizung_Gasleitung"
COMBOS_FILE = Path(r"C:\Users\zaito\futurebeeing\combinations_heat_only.txt")
WORK_DIR    = ESMP_ROOT / "work_folder"
OUT_CSV     = Path(r"C:\Users\zaito\futurebeeing\scenario_gas_only_results.csv")
SOLVER      = "cbc"
LIMIT       = None

GAS_PRICE_EUR_PER_KWH = 0.10
GAS_EMIS_G_PER_KWH    = 202.0

def read_combos(path):
    lines = Path(path).read_text().strip().splitlines()
    combos = []
    for ln in lines[1:]:
        parts = ln.strip().split("_")
        if len(parts) != 4:
            print("  ! skipping malformed line:", ln)
            continue
        size_class, city, heat_cluster, refurb = parts
        combos.append({"size_class": size_class, "city": city,
                       "heat_cluster": float(heat_cluster), "refurb_state": int(refurb)})
    return combos

def prepare_work_folder(combo):
    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR)
    WORK_DIR.mkdir(parents=True)
    shutil.copy(TEMPLATE / "timeseries.json", WORK_DIR / "timeseries.json")
    shutil.copy(TEMPLATE / "weather.json",   WORK_DIR / "weather.json")
    model = json.loads((TEMPLATE / "model.json").read_text())
    patched = False
    for comp in model["components"]:
        if comp.get("category") == "sink" and comp.get("sector") == "heat":
            comp["annualDemand"] = combo["heat_cluster"]
            patched = True
        if comp.get("category") == "converter":
            comp["maxInvestmentCapacity"] = 1000000
        if comp.get("category") == "bus" and "shortage" in comp:
            comp["shortage"]["capacity"] = 1000000
    if not patched:
        raise RuntimeError("heat sink not found in model.json")
    (WORK_DIR / "model.json").write_text(json.dumps(model, indent=2))

def run_optimize():
    cmd = 'uv run esmp optimize --path "' + str(WORK_DIR) + '" --solver ' + SOLVER
    r = subprocess.run(cmd, shell=True, cwd=str(ESMP_ROOT), capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-2000:]); print(r.stderr[-2000:])
        raise RuntimeError("esmp optimize failed")
    return r

def collect_results(combo):
    row = dict(combo)
    with open(WORK_DIR / "summary.csv") as f:
        s = list(csv.DictReader(f))[0]
    row["cost_periodical"] = float(s["Total Periodical Costs"])
    row["total_variable_costs"] = float(s["Total Variable Costs"])
    row["total_energy_demand"] = float(s["Total Energy Demand"])
    with open(WORK_DIR / "components.csv") as f:
        comps = {c["ID"]: c for c in csv.DictReader(f)}
    conv = None
    for c in comps.values():
        if c["type"] == "converter":
            conv = c; break
    if conv:
        row["cap_invest"] = float(conv["capacity/kW"])
        row["production"] = float(conv["output 1/kWh"])
        row["costs_om"] = float(conv["variable costs/CU"])
        row["cost_invest"] = float(conv["investment/kW"])
    imp = 0.0
    for c in comps.values():
        if c["type"] == "source":
            imp += float(c["output 1/kWh"])
    row["energy_import"] = imp
    row["import_cost"] = imp * GAS_PRICE_EUR_PER_KWH
    row["emissions"] = imp * GAS_EMIS_G_PER_KWH
    with open(WORK_DIR / "results.csv") as f:
        rd = csv.DictReader(f)
        cols = {}
        for cn in rd.fieldnames:
            if cn != "date":
                cols[cn] = []
        for r in rd:
            for cn in cols:
                cols[cn].append(float(r[cn]))
    heat_col = None
    for cn in cols:
        if "heat_sink_input1" in cn:
            heat_col = cn; break
    row["heat_demand_profile"] = cols.get(heat_col, [])
    return row

def main():
    combos = read_combos(COMBOS_FILE)
    if LIMIT:
        combos = combos[:LIMIT]
    print("running", len(combos), "combination(s) with solver=" + SOLVER)
    rows = []
    for i, combo in enumerate(combos, 1):
        cid = combo["size_class"] + "_" + combo["city"] + "_" + str(combo["heat_cluster"]) + "_" + str(combo["refurb_state"])
        print("[" + str(i) + "/" + str(len(combos)) + "] " + cid)
        try:
            prepare_work_folder(combo)
            run_optimize()
            rows.append(collect_results(combo))
        except Exception as e:
            print("  FAILED:", e)
    if not rows:
        print("no successful runs"); return
    keys = list(rows[0].keys())
    with open(OUT_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(keys)
        for r in rows:
            w.writerow([json.dumps(v) if isinstance(v, list) else v for v in (r[k] for k in keys)])
    print("wrote", len(rows), "row(s) ->", str(OUT_CSV))

if __name__ == "__main__":
    main()
