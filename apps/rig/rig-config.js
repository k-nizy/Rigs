/* Which rig this machine is - a placeholder, and on a floor it is not
 * this file that answers.
 *
 * `index.html` loads this path relatively, and the kiosk loads the page
 * from the server, so the browser always asks the *server* for it. That
 * is the whole reason this is not a per-machine file any more: one was
 * placed on each rig by Ansible for a while and none of them was ever
 * read, so twelve machines loaded one blank file and every one of them
 * became the same rig. Nothing downstream could see it - from the
 * service's side, twelve rigs reporting as one looks exactly like one
 * very busy rig.
 *
 * On a floor, nginx proxies this path to the service, which answers per
 * caller from RIG_ADDRESSES and sets:
 *
 *     window.RIG_ID      = "RIG-07";
 *     window.RIG_TOKEN   = "<that rig's token>";
 *     window.RIG_SEEN_AS = "10.0.0.17";
 *
 * This file is what is left when nothing does that - a laptop, or a
 * static deploy with no service behind it. Nothing is set, which the rig
 * reads as a demo: it asks for whatever schedule it is offered and
 * authenticates with nothing. A rig that finds a service which *does*
 * identify its rigs, and was not identified by it, refuses to work
 * instead.
 *
 * Both must be set before assets/rig.js runs. index.html loads this
 * first and both are deferred, so document order is execution order.
 */
