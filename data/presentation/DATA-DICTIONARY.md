# Meridian presentation evidence

All bank records and people in the evidence CSV are fictional. No customer identifiers or real account data are present.

The 40-row September 2026 sample covers branch MUM-01, savings, resident individuals. `case_id` is a stable fictional case reference. `review_at` and `activation_at` are ISO timestamps with UTC timezone. `reviewer` is a fictional employee name. These records test timestamp order, not identity authenticity or bank-wide compliance.

The original sample deliberately contains four late approvals (0007, 0014, 0021, 0028) and one equal timestamp (0035). All five fail a strictly-earlier procedure. Corrected evidence retains every case and moves those five approvals 45 minutes before activation. Expected counts must be checked against the actual stored procedure and executor result; they are not prefilled assessment outcomes.

The two simulated instruments deliberately impose incompatible timestamp ordering in the same date range. They are fictional teaching examples, not RBI regulations. The official excerpt is separately labelled and maps to the original stored RBI PDF through official-provenance.json. Do not infer that all RBI KYC requirements have been assessed.
