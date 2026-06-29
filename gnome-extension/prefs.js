import Gio from 'gi://Gio';
import Adw from 'gi://Adw';
import Gtk from 'gi://Gtk';
import {ExtensionPreferences} from 'resource:///org/gnome/Shell/Extensions/js/extensions/prefs.js';

export default class CodelightPreferences extends ExtensionPreferences {
    fillPreferencesWindow(window) {
        const settings = this.getSettings();

        const page  = new Adw.PreferencesPage();
        const group = new Adw.PreferencesGroup({title: 'Daemon connection'});
        page.add(group);
        window.add(page);

        const hostRow = new Adw.EntryRow({title: 'Host'});
        settings.bind('host', hostRow, 'text', Gio.SettingsBindFlags.DEFAULT);
        group.add(hostRow);

        const portRow = new Adw.SpinRow({
            title: 'Port',
            adjustment: new Gtk.Adjustment({
                lower: 1, upper: 65535,
                step_increment: 1, page_increment: 100,
                value: 8765,
            }),
        });
        settings.bind('port', portRow, 'value', Gio.SettingsBindFlags.DEFAULT);
        group.add(portRow);

        const secretRow = new Adw.PasswordEntryRow({title: 'Secret'});
        settings.bind('secret', secretRow, 'text', Gio.SettingsBindFlags.DEFAULT);
        group.add(secretRow);
    }
}
