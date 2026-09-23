# Running it day to day

## When something has not synced

Every failure is visible, attributable to a store, and replayable. Start at the store:

**Shopify Store → the indicators at the top.** Pending and failed counts, webhook errors,
remaining API headroom, when it last synced, when it was last reconciled.

Then, depending on what they say:

| What you see | Where to look | What to do |
|---|---|---|
| Failed queue rows | **Shopify Sync Queue**, filter State = Failed | Read `last_error`; fix the cause, then **Requeue** |
| Webhook errors | **Shopify Event Log**, filter Status = Error | Read the traceback; fix, then **Retry** |
| Pending rows not moving | Check workers are running (`bench doctor`) | Restart the worker |
| API headroom near zero | Shopify is throttling | Nothing — the queue backs off and drains itself |
| Nothing at all recorded | Webhooks may not be registered | Disable and re-enable the store |

## The nightly drift check

Runs at 03:00 by default and repairs anything the webhooks dropped. Run it now with:

```bash
bench --site your-site.localhost shopify-reconcile --store "My Shop"
```

or the **Reconcile Now** button on the store.

It reports how many items it checked, how many had drifted and how many it corrected. **A
correction count that rises run on run is a signal, not a success** — it means webhooks or the
queue are failing upstream and reconciliation is quietly papering over it.

## Which direction repairs run

Always ERPNext → Shopify. ERPNext is the master for stock, and writing ERPNext stock from
Shopify creates a loop with no stable fixed point: each system corrects the other forever.
`inventory_levels/update` is subscribed to for drift detection only.

## Importing a catalogue again

Safe to re-run. Mappings are keyed by `(store, variant GID)`, so a second import updates
rather than duplicates. A crash mid-import resumes from where it stopped.
