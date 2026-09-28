"""
Delivery on the customer's real JD Edwards system -- the recorded route.

Jade does not write to JD Edwards itself until an automated write mechanism
has been validated on the customer's own system. Until then an authorised
person applies the approved change in DEV and records it here; Jade checks
everything first and then verifies what it can LIVE, through the customer's
own AIS connection (Administration > Systems & Connections > JD Edwards):

  * functional.py -- a processing-option change: recorded as applied only
    when the value read back live equals the approved value (or, where the
    connection cannot read it, the person states what they read in JDE,
    labelled as such); the approved test orchestration runs live, or the
    person records the test result against the acceptance criteria.
  * live.py       -- the live reads and orchestration calls, using the
    customer's own connection settings, certificate and credential.
  * readers.py    -- the before-state readers the approval binding uses.
"""
