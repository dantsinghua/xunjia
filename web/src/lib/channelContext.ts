/** Non-secret account generation, frozen into each outgoing HTTP request. */
let selected = ''
export function channelContext(): string { return selected }
export function setChannelContext(value: string): void { selected = value }
