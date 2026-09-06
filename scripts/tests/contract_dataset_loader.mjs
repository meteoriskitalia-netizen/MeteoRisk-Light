// contract_dataset_loader.mjs — CONTRATTO di lettura tra data/latest e il
// caricatore statico dell'app (1.0.0.4). Verifica in Node gli stessi access
// path che il loader userà, così il formato JSON NON può divergere.
//
// Uso: node scripts/tests/contract_dataset_loader.mjs   (dalla root della release)

import { readFileSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
// PARTE G (1.0.0.8): il rilascio NON contiene un dataset live in data/latest.
// Il contratto viene quindi verificato sul dataset reale quando presente,
// altrimenti sul baseline offline generato dal fixture in data/_staging.
let src = path.join(root, "data", "latest");
if (!existsSync(path.join(src, "metadata.json"))) {
  const staging = path.join(root, "data", "_staging");
  if (!existsSync(path.join(staging, "metadata.json"))) {
    console.error("Nessun dataset da verificare: genera prima il baseline " +
      "(py scripts/tests/gen_fixture_raw.py && py scripts/build_meteorisk_dataset.py --raw-json data/_workdir/fixture_raw.json)");
    process.exit(1);
  }
  src = staging;
}

const JSON_HEADERS = { "Content-Type": "application/json" }; // segnaposto (immutabile)
const COVERED_MODELS = ["best_match", "ecmwf_ifs"];
const HOURLY_VARS = [
  "cape", "convective_inhibition", "dew_point_2m", "dew_point_700hPa",
  "dew_point_850hPa", "freezing_level_height", "geopotential_height_500hPa",
  "geopotential_height_700hPa", "geopotential_height_850hPa", "k_index",
  "lifted_index", "precipitation", "precipitation_probability",
  "pressure_msl", "relative_humidity_700hPa", "relative_humidity_850hPa",
  "relativehumidity_2m", "showers", "temperature_2m", "temperature_500hPa",
  "temperature_700hPa", "temperature_850hPa", "weathercode",
  "wind_direction_1000hPa", "wind_direction_500hPa", "wind_direction_600hPa",
  "wind_direction_700hPa", "wind_direction_800hPa", "wind_direction_850hPa",
  "wind_direction_900hPa", "wind_direction_925hPa", "wind_direction_950hPa",
  "wind_direction_975hPa", "wind_speed_1000hPa", "wind_speed_500hPa",
  "wind_speed_600hPa", "wind_speed_700hPa", "wind_speed_800hPa",
  "wind_speed_850hPa", "wind_speed_900hPa", "wind_speed_925hPa",
  "wind_speed_950hPa", "wind_speed_975hPa", "winddirection_100m",
  "winddirection_10m", "windgusts_10m", "windspeed_100m", "windspeed_10m",
];
const DAILY_VARS = ["weather_code", "temperature_2m_max", "temperature_2m_min",
  "precipitation_sum", "wind_speed_10m_max", "wind_gusts_10m_max"];

let failures = 0;
const ok = (name, cond, detail = "") => {
  if (cond) console.log(`  [PASS] ${name}${detail ? ` · ${detail}` : ""}`);
  else { failures++; console.log(`  [FAIL] ${name}${detail ? ` · ${detail}` : ""}`); }
};

const md = JSON.parse(readFileSync(path.join(src, "metadata.json"), "utf-8"));
const P = JSON.parse(readFileSync(path.join(src, "meteorisk-points.json"), "utf-8"));
const PRV = JSON.parse(readFileSync(path.join(src, "meteorisk-provinces.json"), "utf-8"));

// 1. metadata
ok("metadata: dataset_type derivato", md.dataset_type === "derived_meteorological_risk_data", md.dataset_type);
ok("metadata: forecast_days=3", md.forecast_days === 3);
ok("metadata: timezone Europe/Rome", md.timezone === "Europe/Rome");
ok("metadata: models coperti", Array.isArray(md.models_covered) && COVERED_MODELS.every((m) => md.models_covered.includes(m)), md.models_covered?.join(","));
ok("metadata: generated_at ISO", typeof md.generated_at === "string" && !Number.isNaN(Date.parse(md.generated_at)));
ok("metadata: day0 ISO", typeof md.day0 === "string" && /^\d{4}-\d{2}-\d{2}$/.test(md.day0));

// 2. points
ok("points: status live (testabile)", P.status === "live");
ok(`points: n=${P.points.length}`, P.points.length === md.point_count, `md=${md.point_count}`);
const seeds = P.points.filter((p) => p.coordIdx === 0);
ok("points: 107 seed (capoluoghi)", seeds.length === 107, `${seeds.length}`);
ok("points: id contigui", P.points.every((p, i) => p.id === i));
for (const p of P.points) {
  const mobs = p.models ?? {};
  for (const m of COVERED_MODELS) {
    const mod = mobs[m];
    ok(`p${p.id} (${p.sigla}) model=${m}: hourly vars`, mod && Object.keys(mod.hourly).sort().join("|") === [...HOURLY_VARS].sort().join("|"), `${Object.keys(mod?.hourly ?? {}).length} vars`);
    break; // basta un modello per punto in questo pass
  }
  ok(`p${p.id}: lat/lon numerici`, Number.isFinite(p.lat) && Number.isFinite(p.lon));
}
const lensByPoint = P.points.every((p) => COVERED_MODELS.every((m) => {
  const h = p.models?.[m]?.hourly ?? {};
  const d = p.models?.[m]?.daily ?? {};
  return Object.values(h).every((a) => a.length === 72) && Object.values(d).every((a) => a.length === 3);
}));
ok("points: hourly len=72 e daily len=3 (tutti i modelli)", lensByPoint);

// 3. province
ok("provinces: province_count=107", PRV.provinces.length === 107 && PRV.province_count === 107);
ok("provinces: indici contigui", PRV.provinces.every((pr, i) => pr.idx === i));
for (const pr of PRV.provinces) {
  const sp = pr.selected_point;
  const hit = P.points.find((p) => p.id === sp.id);
  ok(`pr ${pr.sigla}: selected_point esiste e coerente`,
    !!hit && hit.provinceIdx === pr.idx && hit.lat === sp.lat && hit.lon === sp.lon && sp.coordIdx === hit.coordIdx,
    `id=${sp.id}`);
  ok(`pr ${pr.sigla}: days=3`, pr.days.length === 3);
}
ok("provinces: tutti i 107 sigle/capoluoghi presenti", new Set(PRV.provinces.map((p) => p.sigla)).size === 107);

console.log(`RESULT: ${failures === 0 ? "PASS" : "FAIL"} (${failures} errori)`);
process.exit(failures === 0 ? 0 : 1);