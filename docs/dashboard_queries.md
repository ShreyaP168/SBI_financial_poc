# Dashboard queries — v9 IDP Supabase store

A cookbook of SQL you can paste into the **Supabase SQL editor** (or any psql) to drive the analytics dashboard. The schema is in `db/schema.sql`; tables: `batches`, `cases`, `case_files`, `agent_runs`, `calculations`, `gating_items`, `flags`, `risk_signals`, `medical_assessments`, `lab_results`.

> **Tip.** All `case_json` and `decision_json` columns are `jsonb`. Use `->`, `->>`, `@>`, and JSONB GIN indexes for ad-hoc questions the schema doesn't already denormalise.

---

## 1.  Batch-level overview

### 1.1  Most recent 10 batches with success rate
```sql
SELECT batch_name, run_date, start_time, end_time,
       total_cases, ok_count, fail_count,
       ROUND(EXTRACT(EPOCH FROM (end_time - start_time))::numeric, 0) AS elapsed_s
  FROM batches
 ORDER BY start_time DESC
 LIMIT 10;
```

### 1.2  Batch contents — per-case row-up
```sql
SELECT c.case_id, c.applicant_name, c.verdict_label, c.overall_risk,
       c.medical_risk, c.fraud_risk, c.elapsed_s
  FROM cases c
  JOIN batches b USING (batch_id)
 WHERE b.batch_name = 'v7-15case-medical-aware-2026-04-27'
 ORDER BY c.elapsed_s DESC;
```

### 1.3  Average per-case latency by batch
```sql
SELECT b.batch_name, COUNT(*) AS n_cases,
       ROUND(AVG(c.elapsed_s)::numeric, 1) AS avg_s,
       ROUND(MAX(c.elapsed_s)::numeric, 1) AS max_s
  FROM cases c
  JOIN batches b USING (batch_id)
 GROUP BY 1
 ORDER BY n_cases DESC;
```

---

## 2.  Verdict + risk distribution

### 2.1  Verdict mix across all batches
```sql
SELECT verdict_label, COUNT(*) AS n,
       ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER (), 1) AS pct
  FROM cases
 WHERE verdict_label IS NOT NULL
 GROUP BY 1
 ORDER BY n DESC;
```

### 2.2  Three-dial roll-up
```sql
SELECT
  COUNT(*)                                 AS total,
  COUNT(*) FILTER (WHERE overall_risk = 'HIGH')     AS overall_high,
  COUNT(*) FILTER (WHERE financial_risk_band = 'HIGH') AS financial_high,
  COUNT(*) FILTER (WHERE medical_risk = 'HIGH')      AS medical_high,
  COUNT(*) FILTER (WHERE fraud_risk = 'HIGH')        AS fraud_high
  FROM (
    SELECT overall_risk, medical_risk, fraud_risk,
           CASE
             WHEN n_flags_critical > 0 OR n_flags_high >= 4 THEN 'HIGH'
             WHEN n_flags_high >= 1 THEN 'MODERATE'
             ELSE 'LOW'
           END AS financial_risk_band
      FROM cases
  ) AS x;
```

### 2.3  Cases that flipped to a different verdict on re-run
*(Useful when you re-extract and want to spot drift.)*
```sql
WITH ranked AS (
  SELECT case_id, verdict_label, b.batch_name, b.start_time,
         ROW_NUMBER() OVER (PARTITION BY case_id ORDER BY b.start_time DESC) AS rn
    FROM cases c
    JOIN batches b USING (batch_id)
)
SELECT c1.case_id,
       c1.verdict_label AS latest, c1.batch_name AS latest_batch,
       c2.verdict_label AS prior,  c2.batch_name AS prior_batch
  FROM ranked c1
  JOIN ranked c2 ON c1.case_id = c2.case_id AND c1.rn = 1 AND c2.rn = 2
 WHERE c1.verdict_label <> c2.verdict_label;
```

---

## 3.  File-level analytics  (the row your dashboard centres on)

### 3.1  All files for a case with classification + agent confidence
```sql
SELECT file_name, page_count, classification_category,
       agents_routed_to, best_agent_confidence
  FROM case_files
 WHERE case_id = 'C684869052'
 ORDER BY file_name;
```

### 3.2  File-type distribution across the whole DB
```sql
SELECT classification_category, COUNT(*) AS n,
       ROUND(AVG(best_agent_confidence)::numeric, 2) AS avg_conf
  FROM case_files
 WHERE classification_category IS NOT NULL
 GROUP BY 1
 ORDER BY n DESC;
```

### 3.3  Files that failed extraction
```sql
SELECT case_id, file_name, extraction_error
  FROM case_files
 WHERE extraction_error IS NOT NULL;
```

### 3.4  Cases missing a salary slip
```sql
SELECT case_id, applicant_name
  FROM cases c
 WHERE NOT EXISTS (
   SELECT 1 FROM case_files f
    WHERE f.case_id = c.case_id
      AND f.classification_category = 'salary_slip'
 );
```

---

## 4.  Per-agent metrics

### 4.1  Average agent confidence by agent
```sql
SELECT agent_name,
       ROUND(AVG(confidence)::numeric, 2) AS avg_conf,
       MIN(confidence)  AS min_conf,
       MAX(confidence)  AS max_conf,
       COUNT(*)          AS n_runs
  FROM agent_runs
 WHERE confidence IS NOT NULL
 GROUP BY 1
 ORDER BY avg_conf DESC;
```

### 4.2  Agents that flagged contradictions
```sql
SELECT agent_name, COUNT(*) AS n_with_contradictions,
       SUM(jsonb_array_length(contradictions))::int AS total_items
  FROM agent_runs
 WHERE jsonb_typeof(contradictions) = 'array'
   AND jsonb_array_length(contradictions) > 0
 GROUP BY 1
 ORDER BY total_items DESC;
```

### 4.3  Pull a specific agent's note for a case
```sql
SELECT agent_name, confidence, notes
  FROM agent_runs
 WHERE case_id = 'C684869052'
 ORDER BY agent_name;
```

---

## 5.  Calculations + provenance

### 5.1  HLV distribution by age band
```sql
SELECT
  (inputs->>'age_band') AS age_band,
  ROUND(AVG(value)::numeric / 10000000, 2) AS avg_hlv_cr,
  COUNT(*) AS n
  FROM calculations
 WHERE name = 'hlv_base'
   AND inputs->>'age_band' IS NOT NULL
 GROUP BY 1
 ORDER BY 1;
```

### 5.2  How many cases use each income cascade rung
```sql
SELECT (inputs->>'primary_source') AS source, COUNT(*) AS n
  FROM calculations
 WHERE name = 'selected_income'
 GROUP BY 1
 ORDER BY n DESC;
```

### 5.3  Show the full provenance trail for one calculation
```sql
SELECT name, value, unit, formula, function, rule, sources, inputs, note
  FROM calculations
 WHERE case_id = 'C684869052'
   AND name = 'selected_income';
```

### 5.4  Cases where engine had to recompute the agent's annual_income
*(Bank-credits fix — Phase 8c.1.)*
```sql
SELECT case_id,
       value                                     AS engine_value,
       inputs->>'agent_reported_annual'          AS agent_value,
       value - (inputs->>'agent_reported_annual')::numeric AS delta
  FROM calculations
 WHERE name = 'selected_income'
   AND method = 'bank_credits_modal_x_12'
   AND value <> (inputs->>'agent_reported_annual')::numeric;
```

---

## 6.  Gating + flags + risk signals

### 6.1  Gating PASS/WARN/FAIL by criterion
```sql
SELECT criterion, status, COUNT(*) AS n
  FROM gating_items
 GROUP BY 1, 2
 ORDER BY 1, 2;
```

### 6.2  Most common HIGH/CRITICAL flag codes
```sql
SELECT code, severity, COUNT(*) AS n
  FROM flags
 WHERE severity IN ('CRITICAL','HIGH')
 GROUP BY 1, 2
 ORDER BY n DESC
 LIMIT 20;
```

### 6.3  Top 10 reconciler risk signals across all cases
```sql
SELECT code, severity, COUNT(*) AS n
  FROM risk_signals
 GROUP BY 1, 2
 ORDER BY n DESC
 LIMIT 10;
```

### 6.4  Cases with COTININE_SMOKER_MISMATCH (fraud)
```sql
SELECT c.case_id, c.applicant_name, m.cotinine_ng_ml, m.smoker_declared
  FROM cases c
  JOIN medical_assessments m USING (case_id)
 WHERE m.cotinine_smoker_mismatch = TRUE
 ORDER BY m.cotinine_ng_ml DESC;
```

---

## 7.  Medical underwriting

### 7.1  BMI-category distribution
```sql
SELECT bmi_category, COUNT(*) AS n,
       ROUND(AVG(bmi)::numeric, 1) AS avg_bmi
  FROM medical_assessments
 WHERE bmi_category IS NOT NULL
 GROUP BY 1
 ORDER BY 2 DESC;
```

### 7.2  Smokers and their cotinine readings
```sql
SELECT case_id, smoker_declared,
       cotinine_ng_ml, cotinine_smoker_mismatch
  FROM medical_assessments
 WHERE smoker_declared IS NOT NULL
   AND smoker_declared <> 'Unknown'
 ORDER BY cotinine_ng_ml DESC NULLS LAST;
```

### 7.3  Medical document compliance by case
```sql
SELECT case_id, required_test_count, received_test_count,
       test_compliance_pct, missing_tests
  FROM medical_assessments
 ORDER BY test_compliance_pct ASC NULLS FIRST;
```

### 7.4  Cases with HbA1c in pre-diabetic / diabetic range
```sql
SELECT l.case_id, c.applicant_name,
       l.value_num, l.unit, l.status, l.reference_range
  FROM lab_results l
  JOIN cases c USING (case_id)
 WHERE LOWER(l.test_name) LIKE 'hba1c%'
   AND l.value_num >= 5.7
 ORDER BY l.value_num DESC;
```

### 7.5  Top-10 abnormal labs by frequency
```sql
SELECT test_name, status, COUNT(*) AS n,
       ROUND(AVG(value_num)::numeric, 2) AS avg_value
  FROM lab_results
 WHERE status IN ('HIGH','LOW','ABNORMAL')
 GROUP BY 1, 2
 ORDER BY n DESC
 LIMIT 10;
```

### 7.6  Lab-table for one case (the renderer's "Laboratory Results" section)
```sql
SELECT category, test_name, value_text, unit, reference_range, status
  FROM lab_results
 WHERE case_id = 'C684869052'
 ORDER BY
   CASE category
     WHEN 'hematology' THEN 1 WHEN 'lipid' THEN 2 WHEN 'liver' THEN 3
     WHEN 'kidney' THEN 4 WHEN 'metabolic' THEN 5 WHEN 'urine' THEN 6
     WHEN 'serology' THEN 7 ELSE 99 END,
   test_name;
```

### 7.7  Cases with BP Stage-1 hypertension (≥140/90) but no declared hypertension
```sql
SELECT c.case_id, c.applicant_name,
       m.bp_systolic_mmhg, m.bp_diastolic_mmhg
  FROM medical_assessments m
  JOIN cases c USING (case_id)
 WHERE (m.bp_systolic_mmhg >= 140 OR m.bp_diastolic_mmhg >= 90)
   AND NOT (c.case_json::text ILIKE '%hypertension%active%');
```

---

## 8.  Free-form JSONB drill-downs

### 8.1  Pull the medical block for one case (full JSON)
```sql
SELECT case_json -> 'medical'
  FROM cases
 WHERE case_id = 'C684869052';
```

### 8.2  Family-history relations across the dataset
```sql
SELECT
  fh -> 'relationship' AS relation,
  COUNT(*) AS n
  FROM cases c,
       jsonb_array_elements(c.case_json -> 'medical' -> 'family_history') fh
 GROUP BY 1
 ORDER BY n DESC;
```

### 8.3  All "personal_medical_history" conditions across cases
```sql
SELECT
  ph ->> 'condition' AS condition,
  ph ->> 'status'    AS status,
  COUNT(*) AS n
  FROM cases c,
       jsonb_array_elements(c.case_json -> 'medical' -> 'personal_medical_history') ph
 GROUP BY 1, 2
 ORDER BY n DESC;
```

### 8.4  Reconciler `reconciler_notes` for a case
```sql
SELECT case_json -> 'reconciliation' ->> 'reconciler_notes'
  FROM cases
 WHERE case_id = 'C684869052';
```

---

## 9.  Dashboard "summary tiles" — paste straight into a Supabase view

```sql
-- Drop+create whenever the schema changes
DROP VIEW IF EXISTS v_dashboard_summary CASCADE;

CREATE VIEW v_dashboard_summary AS
SELECT
  c.batch_id,
  b.batch_name,
  b.run_date,
  c.case_id,
  c.applicant_name,
  c.pan,
  c.age,
  c.gender,
  c.occupation,
  c.proposed_sa,
  c.verdict_label,
  c.overall_risk,
  c.medical_risk,
  c.fraud_risk,
  c.recon_stp,
  c.health_score,
  c.bmi,
  c.bmi_category,
  c.n_flags_critical,
  c.n_flags_high,
  c.n_contradictions,
  c.n_risk_signals,
  c.elapsed_s,
  m.cotinine_smoker_mismatch,
  m.test_compliance_pct,
  m.missing_tests
  FROM cases c
  LEFT JOIN batches b              USING (batch_id)
  LEFT JOIN medical_assessments m  USING (case_id);
```

Then a simple `SELECT * FROM v_dashboard_summary WHERE batch_name = '…'` powers the main dashboard table.

---

## 10.  Re-run a single case through the engine without re-extracting

When a rule or template changes and you don't want to spend LLM tokens re-extracting:

```bash
python -c "
import json
from pathlib import Path
from engine import run_engine
from renderer import render_case_sheet
from db import DBWriter

rules = json.loads(Path('config/uw_rules.json').read_text(encoding='utf-8'))
case = json.loads(Path('output/<CASE_ID>/<CASE_ID>_case.json').read_text(encoding='utf-8'))
report = json.loads(Path('output/<CASE_ID>/<CASE_ID>_extraction_report.json').read_text(encoding='utf-8'))

decision = run_engine(case, rules, proposed_sa=10_000_000)

Path('output/<CASE_ID>/<CASE_ID>_decision.json').write_text(
    json.dumps(decision, indent=2, default=str, ensure_ascii=False), encoding='utf-8')
render_case_sheet(decision, 'output/<CASE_ID>/<CASE_ID>_case_sheet.pdf')

with DBWriter() as db:
    db.persist_case(case_json=case, decision_json=decision,
                    extraction_report=report,
                    case_id='<CASE_ID>', proposed_sa=10_000_000,
                    elapsed_s=None, source_dir='output/<CASE_ID>')
"
```
