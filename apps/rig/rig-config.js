/* Per-machine configuration. Ansible writes this file on each rig from
 * the same source as /etc/rig/id, and it is the only file that differs
 * between the twelve machines.
 *
 * Shipped blank on purpose. A rig with no config is a demo: it asks for
 * whatever schedule the server offers and authenticates with nothing,
 * which is right on a laptop and wrong on a floor. `/api/health` reports
 * whether the service is enforcing tokens, so a floor that is running
 * unconfigured is visible rather than assumed.
 *
 * On a real rig this file reads:
 *
 *     window.RIG_ID    = "RIG-07";
 *     window.RIG_TOKEN = "<the token Ansible generated for RIG-07>";
 *
 * Both must be set before assets/rig.js runs. index.html loads this
 * first and both are deferred, so document order is execution order.
 */
