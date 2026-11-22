import Gio from 'gi://Gio';
import Meta from 'gi://Meta';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const INTERFACE = `<node><interface name="local.studiowright.Foreground">
<method name="Status"><arg type="s" direction="out"/></method>
<method name="List"><arg type="u" direction="in"/><arg type="s" direction="out"/></method>
<method name="Activate"><arg type="u" direction="in"/><arg type="u" direction="in"/><arg type="s" direction="out"/></method>
</interface></node>`;

export default class FocusExtension extends Extension {
    enable() {
        this.object = Gio.DBusExportedObject.wrapJSObject(INTERFACE, this);
        this.object.export(Gio.DBus.session, '/local/studiowright/Foreground');
        this.owner = Gio.bus_own_name_on_connection(Gio.DBus.session,
            'local.studiowright.Foreground', Gio.BusNameOwnerFlags.NONE, null, null);
    }
    disable() {
        if (this.owner) Gio.bus_unown_name(this.owner);
        if (this.object) this.object.unexport();
    }
    Status() {
        const w = global.display.focus_window;
        return JSON.stringify({workspace: global.workspace_manager.get_active_workspace_index(),
            pid: w?.get_pid() ?? null, id: w?.get_stable_sequence() ?? null,
            window_workspace: w?.get_workspace()?.index() ?? null});
    }
    List(pid) {
        return JSON.stringify(global.get_window_actors().map(a => a.meta_window).filter(w =>
            w && w.get_pid() === pid && !w.skip_taskbar &&
            w.get_window_type() === Meta.WindowType.NORMAL &&
            /chrome|chromium/i.test(w.get_wm_class() || '')).map(w => ({
                id: w.get_stable_sequence(), workspace: w.get_workspace()?.index() ?? -1,
                title: w.get_title(), showing: w.showing_on_its_workspace(),
                x: w.get_frame_rect().x, y: w.get_frame_rect().y,
                width: w.get_frame_rect().width, height: w.get_frame_rect().height
            })));
    }
    Activate(pid, id) {
        const found = global.get_window_actors().map(a => a.meta_window).filter(w =>
            w && w.get_pid() === pid && w.get_stable_sequence() === id && !w.skip_taskbar &&
            w.get_window_type() === Meta.WindowType.NORMAL &&
            /chrome|chromium/i.test(w.get_wm_class() || ''));
        if (found.length !== 1 || !found[0].get_workspace())
            return JSON.stringify({ok: false, reason: 'window_identity_unconfirmed'});
        const win = found[0];
        const timestamp = global.get_current_time() || global.display.get_last_user_time();
        if (win.get_workspace() === global.workspace_manager.get_active_workspace())
            win.activate(timestamp);
        else
            win.get_workspace().activate_with_focus(win, timestamp);
        if (win.get_workspace() === global.workspace_manager.get_active_workspace() &&
            global.display.focus_window !== win)
            win.focus(timestamp);
        return JSON.stringify({ok: true});
    }
}
