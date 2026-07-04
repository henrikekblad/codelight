import Adw from 'gi://Adw';
import {ExtensionPreferences} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

export default class CodelightPreferences extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        const settings = this.getSettings();

        const page  = new Adw.PreferencesPage();
        const group = new Adw.PreferencesGroup({
            title: 'Codelight',
            description: 'Codelight connects automatically via D-Bus — no configuration needed.\n\nRun codelight.py on this machine to start the daemon.',
        });

        const permRow = new Adw.SwitchRow({
            title: 'Show permission prompts',
            subtitle: 'Approve Claude Code permission requests from a desktop notification. Requires the companion to run with --remote-permissions.',
        });
        settings.bind('permission-prompts', permRow, 'active', 0 /* Gio.SettingsBindFlags.DEFAULT */);
        group.add(permRow);

        page.add(group);
        window.add(page);
    }
}
