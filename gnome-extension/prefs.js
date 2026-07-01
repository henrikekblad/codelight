import Adw from 'gi://Adw';
import {ExtensionPreferences} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

export default class CodelightPreferences extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        const page  = new Adw.PreferencesPage();
        const group = new Adw.PreferencesGroup({
            title: 'Codelight',
            description: 'Codelight connects automatically via D-Bus — no configuration needed.\n\nRun codelight.py on this machine to start the daemon.',
        });
        page.add(group);
        window.add(page);
    }
}
