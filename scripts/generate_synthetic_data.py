from __future__ import annotations

import argparse
import csv
import hashlib
import json
import random
import tempfile
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SEED = 20261011
SNAPSHOT = datetime(2026, 10, 11, 8, 0, tzinfo=UTC)
ROW_COUNT = 10_000

PARAMETERS = [
    "bp_systolic_mm_hg",
    "bp_diastolic_mm_hg",
    "heart_rate_bpm",
    "spo2_pct",
    "temperature_c",
    "respiratory_rate_bpm",
    "weight_kg",
    "bmi",
    "creatinine_mg_dl",
    "egfr_ml_min_1_73m2",
    "glucose_mg_dl",
    "hemoglobin_g_dl",
    "sodium_mmol_l",
    "potassium_mmol_l",
    "phq9_score",
]

METADATA = [
    "customer_id",
    "age",
    "sex_at_birth",
    "conditions",
    "missed_appointment",
    "consent_status",
    "last_contact_at",
    "measured_at",
]


def clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def choose_conditions(rng: random.Random, age: int) -> list[str]:
    conditions: list[str] = []
    if rng.random() < 0.25 + max(age - 45, 0) * 0.006:
        conditions.append("hypertension")
    if rng.random() < 0.15 + max(age - 45, 0) * 0.004:
        conditions.append("type_2_diabetes")
    if rng.random() < 0.06 + max(age - 55, 0) * 0.003:
        conditions.append("chronic_kidney_disease")
    if rng.random() < 0.08 + max(age - 55, 0) * 0.002:
        conditions.append("copd")
    if rng.random() < 0.05 + max(age - 60, 0) * 0.002:
        conditions.append("heart_failure")
    if rng.random() < 0.16:
        conditions.append("depression")
    return conditions or ["preventive_follow_up"]


def make_row(rng: random.Random, index: int) -> dict[str, Any]:
    age = round(clamp(rng.triangular(18, 90, 54), 18, 90))
    sex = rng.choices(["female", "male", "intersex"], weights=[49.7, 49.7, 0.6], k=1)[0]
    conditions = choose_conditions(rng, age)
    has = set(conditions)

    bmi = clamp(rng.normalvariate(26.2 + (2.5 if "type_2_diabetes" in has else 0), 5.0), 15, 52)
    height_m = clamp(rng.normalvariate(1.66 if sex == "female" else 1.74, 0.09), 1.42, 2.02)
    weight = bmi * height_m**2

    systolic = rng.normalvariate(119 + age * 0.18, 13)
    if "hypertension" in has:
        systolic += rng.normalvariate(21, 9)
    diastolic = rng.normalvariate(74 + age * 0.04, 9)
    if "hypertension" in has:
        diastolic += rng.normalvariate(11, 5)

    heart_rate = rng.normalvariate(74, 11) + (5 if "heart_failure" in has else 0)
    spo2 = (
        rng.normalvariate(97.1, 1.4)
        - (4.0 if "copd" in has else 0)
        - (1.5 if "heart_failure" in has else 0)
    )
    respiratory_rate = rng.normalvariate(16.5, 2.7) + (3 if "copd" in has else 0)

    renal_burden = 0.0
    if "chronic_kidney_disease" in has:
        renal_burden += rng.uniform(0.8, 3.5)
    renal_burden += max(age - 65, 0) * 0.01
    creatinine = clamp(rng.normalvariate(0.82 + renal_burden, 0.22 + renal_burden * 0.12), 0.3, 8.0)
    egfr = clamp(118 - age * 0.55 - max(creatinine - 0.8, 0) * 26 + rng.normalvariate(0, 7), 5, 140)

    glucose = rng.normalvariate(98, 18)
    if "type_2_diabetes" in has:
        glucose += rng.normalvariate(95, 48)
    hemoglobin_base = 13.4 if sex == "female" else 14.4
    hemoglobin = rng.normalvariate(hemoglobin_base - renal_burden * 0.7, 1.0)
    sodium = rng.normalvariate(139, 3.0) - (1.5 if "heart_failure" in has else 0)
    potassium = rng.normalvariate(4.15 + renal_burden * 0.16, 0.38)
    phq9 = round(clamp(rng.normalvariate(10 if "depression" in has else 3.5, 4.2), 0, 27))

    # Rare synthetic-only edge cases exercise urgent and data-quality gates.
    if index % 997 == 0:
        spo2 = rng.uniform(84, 89)
    if index % 1499 == 0:
        systolic = rng.uniform(182, 205)
    if index % 1877 == 0:
        phq9 = rng.randint(20, 27)
    if index % 2137 == 0:
        diastolic = systolic + rng.uniform(1, 5)

    measured_at = SNAPSHOT - timedelta(days=rng.randint(0, 45), hours=rng.randint(0, 23))
    last_contact = SNAPSHOT - timedelta(days=rng.randint(7, 365))
    return {
        "customer_id": f"C{index:05d}",
        "age": age,
        "sex_at_birth": sex,
        "conditions": "|".join(conditions),
        "missed_appointment": str(rng.random() < 0.16).lower(),
        "consent_status": "synthetic_opted_in" if rng.random() < 0.94 else "synthetic_withheld",
        "last_contact_at": last_contact.isoformat().replace("+00:00", "Z"),
        "measured_at": measured_at.isoformat().replace("+00:00", "Z"),
        "bp_systolic_mm_hg": round(clamp(systolic, 70, 230), 1),
        "bp_diastolic_mm_hg": round(clamp(diastolic, 40, 160), 1),
        "heart_rate_bpm": round(clamp(heart_rate, 35, 180), 1),
        "spo2_pct": round(clamp(spo2, 70, 100), 1),
        "temperature_c": round(clamp(rng.normalvariate(36.8, 0.45), 34, 41), 1),
        "respiratory_rate_bpm": round(clamp(respiratory_rate, 8, 40), 1),
        "weight_kg": round(clamp(weight, 35, 220), 1),
        "bmi": round(bmi, 1),
        "creatinine_mg_dl": round(creatinine, 2),
        "egfr_ml_min_1_73m2": round(egfr, 1),
        "glucose_mg_dl": round(clamp(glucose, 45, 500), 1),
        "hemoglobin_g_dl": round(clamp(hemoglobin, 5, 20), 1),
        "sodium_mmol_l": round(clamp(sodium, 115, 165), 1),
        "potassium_mmol_l": round(clamp(potassium, 2.2, 7.5), 2),
        "phq9_score": phq9,
    }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate(path: Path) -> dict[str, Any]:
    ids: set[str] = set()
    counts: Counter[str] = Counter()
    rows = 0
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != METADATA + PARAMETERS:
            raise ValueError("CSV columns do not match the 8 metadata + 15 parameter contract")
        for row in reader:
            rows += 1
            customer_id = row["customer_id"]
            if customer_id in ids:
                raise ValueError(f"Duplicate customer_id: {customer_id}")
            ids.add(customer_id)
            if any(row[field] == "" for field in reader.fieldnames):
                raise ValueError(f"Null/blank value in {customer_id}")
            counts[f"consent:{row['consent_status']}"] += 1
            counts[f"missed:{row['missed_appointment']}"] += 1
    if rows != ROW_COUNT:
        raise ValueError(f"Expected {ROW_COUNT} rows, found {rows}")
    return {"rows": rows, "unique_ids": len(ids), "counts": dict(sorted(counts.items()))}


def generate(output: Path, manifest_path: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    # Reproducibility is the requirement here; this RNG never generates secrets.
    rng = random.Random(SEED)  # noqa: S311
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", delete=False, dir=output.parent, suffix=".tmp"
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=METADATA + PARAMETERS, lineterminator="\n")
        writer.writeheader()
        for index in range(1, ROW_COUNT + 1):
            writer.writerow(make_row(rng, index))
        temporary = Path(handle.name)
    temporary.replace(output)
    stats = validate(output)
    manifest = {
        "synthetic_only": True,
        "clinical_use_approved": False,
        "generator_seed": SEED,
        "snapshot_at": SNAPSHOT.isoformat(),
        "rows": ROW_COUNT,
        "metadata_fields": METADATA,
        "clinical_style_parameters": PARAMETERS,
        "parameter_count": len(PARAMETERS),
        "sha256": sha256(output),
        "validation": stats,
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate reproducible synthetic MediCare data")
    parser.add_argument("--output", type=Path, default=Path("data/synthetic_customers.csv"))
    parser.add_argument(
        "--manifest", type=Path, default=Path("data/synthetic_customers.manifest.json")
    )
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    if args.validate_only:
        print(json.dumps(validate(args.output), indent=2, sort_keys=True))
    else:
        generate(args.output, args.manifest)
        print(
            json.dumps(
                {
                    "output": str(args.output),
                    "manifest": str(args.manifest),
                    **validate(args.output),
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
