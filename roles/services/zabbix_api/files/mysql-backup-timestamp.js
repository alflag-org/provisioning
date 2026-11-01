// The backup writer records whole-second UTC ISO timestamps.
var status = JSON.parse(value);
if (!status || typeof status !== 'object' || Array.isArray(status) || status.unavailable === true ||
        !Object.prototype.hasOwnProperty.call(status, 'last_success') ||
        !Object.prototype.hasOwnProperty.call(status, 'last_failure')) {
    throw 'Backup completion metadata is unavailable';
}
var recorded = status[field];
if (recorded === null) {
    return 0;
}
if (typeof recorded !== 'string' || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(recorded)) {
    throw 'Invalid backup timestamp';
}
var seconds = Date.parse(recorded);
if (!isFinite(seconds) || seconds <= 0 || new Date(seconds).toISOString().replace('.000Z', 'Z') !== recorded) {
    throw 'Invalid backup timestamp';
}
return Math.floor(seconds / 1000);
