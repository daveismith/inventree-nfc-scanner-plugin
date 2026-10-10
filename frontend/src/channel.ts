/**
 * The fleet page tells the scanner service, in every tab of this browser, that it has just
 * deployed firmware, so a USB scanner connected here is offered the update at once rather than
 * at its next periodic check-in. Its own module, so the fleet page need not load the service.
 */

export const DEPLOY_CHANNEL = 'inventree-nfc-scanner-deploy';

export function announceDeployment() {
  if (typeof BroadcastChannel === 'undefined') return;
  const channel = new BroadcastChannel(DEPLOY_CHANNEL);
  channel.postMessage({ type: 'deployed' });
  channel.close();
}
