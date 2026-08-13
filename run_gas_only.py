"""
Gas-only scenario runner for the FutureBEEing NL pipeline.

For each building-combination in combinations_heat_only.txt:
  1. read the combination -> (size_class, city, heat_cluster, refurb_state)
  2. load a gas-only model.json template and patch in the heat demand + refurb-driven values
  3. write model/timeseries/weather JSONs into a work folder
  4. run `esmp optimize --path work_folder --solver cbc`
  5. read summary.csv + components.csv + results.csv, pull the values listed
  6. append one row (scalars + hourly profiles as lists) to the output table
  7. save as CSV/JSON

This is a SKELETON with the file formats wired to the real esmp outputs you confirmed.
The two spots that need YOUR input from a real run are marked  # >>> CONFIRM <<< .
Start by running it on ONE combination (LIMIT = 1) before the whole file.
"""

import os, sys, json, shutil, subprocess, csv, ast
from pathlib import Path

# ---------------------------------------------------------------------------
# CONFIG  -- edit these paths to your machine
# ---------------------------------------------------------------------------
ESMP_ROOT   = Path(r"C:\Users\zaito\esmp")                       # the esmp repo
TEMPLATE    = ESMP_ROOT / "tests/scenarios/Waerme_Gasheizung_Gasleitung"  # gas-only example we know runs
COMBOS_FILE = Path(r"C:\Users\zaito\futurebeeing\combinations_heat_only.txt")
WORK_DIR    = ESMP_ROOT / "work_folder"                          # scratch: JSONs + results per run
OUT_CSV     = Path(r"C:\Users\zaito\futurebeeing\scenario_gas_only_results.csv")
SOLVER      = "cbc"
LIMIT       = 1        # <-- start with 1. Set to None to run ALL combinations once it works.

# NL constants for the computed values (emissions / import cost) 
GAS_PRICE_EUR_PER_KWH = 0.15    # >>> CONFIRM <<<  NL gas price EUR/kWh
GAS_EMIS_G_PER_KWH    = 201.0    # >>> CONFIRM <<<  NL gas carrier gCO2/kWh

# ---------------------------------------------------------------------------
# 1. parse the combination file
#    header: size_class_nearest_city_heat_cluster_refurbishment_state
#    values are underscore-free by design, so split on "_" gives exactly 4 fields.
# ---------------------------------------------------------------------------
def read_combos(path):
    lines = Path(path).read_text().strip().splitlines()
    combos = []
    for ln in lines[1:]:                       # skip header
        parts = ln.strip().split("_")
        if len(parts) != 4:
            print(f"  ! skipping malformed line: {ln!r}")
            continue
        size_class, city, heat_cluster, refurb = parts
        combos.append({
            "size_class": size_class,
            "city": city,
            "heat_cluster": float(heat_cluster),   # annual heat demand kWh for this cluster
            "refurb_state": int(refurb),           # 1/2/3
        })
    return combos

# ---------------------------------------------------------------------------
# 2. build the three JSONs for one combination
#    We start from the known-good template and patch it, rather than build from scratch.
# ---------------------------------------------------------------------------
def prepare_work_folder(combo):
    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR)
    WORK_DIR.mkdir(parents=True)

    # copy the template's timeseries + weather as-is for now (they define the hourly SHAPE).
    # The heat DEMAND LEVEL is what we scale per combination.  >>> CONFIRM <<< how the
    # template encodes demand: is the sink's nominal_value in model.json, or a series in
    # timeseries.json?  Open model.json and search for the heat sink to decide.
    shutil.copy(TEMPLATE / "timeseries.json", WORK_DIR / "timeseries.json")
    shutil.copy(TEMPLATE / "weather.json",   WORK_DIR / "weather.json")

    model = json.loads((TEMPLATE / "model.json").read_text())

    # --- patch heat demand ---------------------------------------------------
    # The heat sink carries "annualDemand" (kWh/yr) plus a "loadProfile" and
    # "buildingClass" that shape the hourly curve. Per combination we set the
    # ANNUAL kWh to this cluster's value; esmp builds the hourly profile from it.
    patched = False
    for comp in model["components"]:
        if comp.get("category") == "sink" and comp.get("sector") == "heat":
            comp["annualDemand"] = combo["heat_cluster"]
            # to heating temperature; here we could also vary buildingClass by refurb
            # if he wants the CURVE to change, not just the total. For now: total only.
            patched = True
    if not patched:
        raise RuntimeError("heat sink not found in model.json - check structure")

    # NOTE: refurb_state -> heating temperature (60/50/35 C) only matters for the
    # ASHP scenario (COP depends on it). For GAS-ONLY the efficiency is fixed at 1,
    # so refurb state does NOT change the gas result except through demand level.
    # We keep refurb_state in the output row for the combination ID, but it does
    # not patch anything in the gas model. (This will matter for scenario 4.)

    # sync the run period to a full year to match the combination's annual kWh
    # (template already uses periods 8760 / full-year dates, so nothing to change)

    (WORK_DIR / "model.json").write_text(json.dumps(model, indent=2))

# ---------------------------------------------------------------------------
# 3. run esmp on the work folder
# ---------------------------------------------------------------------------
def run_optimize():
    cmd = f'uv run esmp optimize --path "{WORK_DIR}" --solver {SOLVER}'
    r = subprocess.run(cmd, shell=True, cwd=str(ESMP_ROOT),
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout[-2000:]); print(r.stderr[-2000:])
        raise RuntimeError("esmp optimize failed")
    return r

# ---------------------------------------------------------------------------
# 4. collect results from the CSVs esmp wrote into the work folder
# ---------------------------------------------------------------------------
def collect_results(combo):
    row = dict(combo)

    # --- summary.csv : system scalars ---
    with open(WORK_DIR / "summary.csv") as f:
        s = list(csv.DictReader(f))[0]
    row["cost_periodical"] = float(s["Total Periodical Costs"])
    row["total_variable_costs"] = float(s["Total Variable Costs"])
    row["total_energy_demand"]  = float(s["Total Energy Demand"])

    # --- components.csv : per-component values ---
    with open(WORK_DIR / "components.csv") as f:
        comps = {c["ID"]: c for c in csv.DictReader(f)}
    # the gas converter (called ID_chp_converter in the template)  >>> CONFIRM ID <<<
    conv = next((c for c in comps.values() if c["type"] == "converter"), None)  # ID_chp_converter in template
    if conv:
        row["cap_invest"]  = float(conv["capacity/kW"])
        row["production"]  = float(conv["output 1/kWh"])
        row["costs_om"]    = float(conv["variable costs/CU"])
        row["cost_invest"] = float(conv["investment/kW"])
    # gas import = sum of the shortage sources' output
    imp = sum(float(c["output 1/kWh"]) for c in comps.values()
              if c["type"] == "source")
    row["energy_import"] = imp

    # --- computed values (not solver outputs) ---
    row["import_cost"] = imp * GAS_PRICE_EUR_PER_KWH
    row["emissions"]   = imp * GAS_EMIS_G_PER_KWH

    # --- results.csv : hourly profiles, stored as lists ---
    with open(WORK_DIR / "results.csv") as f:
        rd = csv.DictReader(f)
        cols = {c: [] for c in rd.fieldnames if c != "date"}
        for r in rd:
            for c in cols:
                cols[c].append(float(r[c]))
    # heat demand profile = the heat sink input  >>> CONFIRM column name <<<
    heat_col = next((c for c in cols if "heat_sink_input1" in c), None)
    row["heat_demand_profile"] = cols.get(heat_col, [])

    return row

# ---------------------------------------------------------------------------
# main loop
# ---------------------------------------------------------------------------
def main():
    combos = read_combos(COMBOS_FILE)
    if LIMIT:
        combos = combos[:LIMIT]
    print(f"running {len(combos)} combination(s) with solver={SOLVER}")

    rows = []
    for i, combo in enumerate(combos, 1):
        cid = f'{combo["size_class"]}_{combo["city"]}_{combo["heat_cluster"]}_{combo["refurb_state"]}'
        print(f"[{i}/{len(combos)}] {cid}")
        try:
            prepare_work_folder(combo)
            run_optimize()
            rows.append(collect_results(combo))
        except Exception as e:
            print(f"  FAILED: {e}")

    if not rows:
        print("no successful runs"); return

    # write table. profiles are lists -> store as JSON strings in the CSV cell.
    keys = list(rows[0].keys())
    with open(OUT_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(keys)
        for r in rows:
            w.writerow([json.dumps(v) if isinstance(v, list) else v for v in (r[k] for k in keys)])
    print(f"wrote {len(rows)} row(s) -> {OUT_CSV}")

if __name__ == "__main__":
    main()
