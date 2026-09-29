"""
The agents make the approved changes in the customer's DEV system.

  settings  -- per customer, entered in Administration and stored in the
               database: the JD Edwards web client and Web OMW addresses,
               the dedicated DEV write user (encrypted, separate from the
               read-only discovery user) and the on/off switches for agent
               execution, per customer and per capability.
  routes    -- which route each approved item takes: AIS form requests,
               the web client in an agent-driven browser, or a person.
  ais       -- the AIS executor: form requests that drive the JD Edwards
               configuration applications for exactly one approved item.
  browser   -- the browser executor: an agent operating the customer's web
               client and Web OMW, with a screenshot of every step.
  runner    -- runs an approved change set item by item through the
               delivery gate, with a live read before and after each item.

The AIS address, its certificate, the environment and the path code come
from the customer's JD Edwards connection (Administration > Systems &
Connections > JDE); nothing about a customer's JD Edwards comes from the
server's environment.
"""
