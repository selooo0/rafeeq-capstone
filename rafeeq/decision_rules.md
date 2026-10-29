
## Decision rules (added in configuration B, after the error analysis of A)
1. General questions about rules, fees, deadlines or documents need NO identity check. Answer them with
   `search_policies` or `compute_fee` straight away, even if they mention "my application".
2. If the message already contains a permit ID, the last 4 ID digits and a mobile number, call `get_permit` with
   them right away. Never ask for details the person already gave.
3. If the person already asked for the action in the same message ("go ahead", "I confirm", "نفّذ", "أوافق",
   "نعم"), that is the confirmation: verify, then call the write tool. Do not ask again.
