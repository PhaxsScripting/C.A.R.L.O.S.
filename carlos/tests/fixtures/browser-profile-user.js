// Only copied into a disposable browser-test profile; never a user profile.
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.aboutwelcome.enabled", false);
// Firefox provides this automation opt-out; do not accept terms or dismiss
// consent dialogs on the user's behalf or alter any existing browser profile.
user_pref("browser.preonboarding.enabled", false);
user_pref("browser.aboutwelcome.experimentsGate.enabled", false);
user_pref("termsofuse.bypassNotification", true);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("browser.newtabpage.enabled", false);
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("toolkit.telemetry.enabled", false);
user_pref("network.captive-portal-service.enabled", false);
user_pref("network.connectivity-service.enabled", false);
