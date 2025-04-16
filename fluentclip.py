#!/usr/bin/env python3
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
gi.require_version('Notify', '0.7')
from gi.repository import Gtk, Gdk, GdkPixbuf, GLib, Pango, GObject, Notify
import cairo
import os
import json
import time
from datetime import datetime
import tempfile
from PIL import Image
import io
import base64
import signal
import subprocess
import dbus
import dbus.service
import dbus.mainloop.glib

# Check if we have python-xlib for keyboard shortcuts
try:
    from Xlib import X, XK, display
    from Xlib.ext import record
    from Xlib.protocol import rq
    XLIB_AVAILABLE = True
except ImportError:
    XLIB_AVAILABLE = False
    print("python-xlib not available; global hotkey will be disabled")

class HotKeyManager:
    def __init__(self, callback):
        self.callback = callback
        self.running = False
        self.display = None
        self.ctx = None
        self.super_pressed = False

    def start(self):
        if not XLIB_AVAILABLE:
            return False
            
        try:
            self.display = display.Display()
            self.root = self.display.screen().root
            
            # Monitor key events
            self.ctx = self.display.record_create_context(
                0,
                [record.AllClients],
                [{
                    'core_requests': (0, 0),
                    'core_replies': (0, 0),
                    'ext_requests': (0, 0, 0, 0),
                    'ext_replies': (0, 0, 0, 0),
                    'delivered_events': (0, 0),
                    'device_events': (X.KeyPress, X.KeyRelease),
                    'errors': (0, 0),
                    'client_started': False,
                    'client_died': False,
                }]
            )
            
            self.running = True
            
            # Start in a separate thread
            import threading
            self.thread = threading.Thread(target=self._event_loop)
            self.thread.daemon = True
            self.thread.start()
            
            return True
        except Exception as e:
            print(f"Error setting up hotkey manager: {e}")
            return False

    def _event_loop(self):
        if self.ctx:
            self.display.record_enable_context(self.ctx, self._process_event)
            self.display.record_free_context(self.ctx)

    def _process_event(self, reply):
        if not self.running:
            return
            
        data = reply.data
        while len(data):
            event, data = rq.EventField(None).parse_binary_value(
                data, self.display.display, None, None)
                
            if event.type == X.KeyPress:
                keycode = event.detail
                keysym = self.display.keycode_to_keysym(keycode, 0)
                
                # Check for Super key (Windows key)
                if keysym in (XK.XK_Super_L, XK.XK_Super_R):
                    self.super_pressed = True
                # Check for V key while Super is pressed
                elif self.super_pressed and keysym == XK.XK_v:
                    # Use GLib.idle_add to safely call the callback from the main thread
                    GLib.idle_add(self.callback)
            
            elif event.type == X.KeyRelease:
                keycode = event.detail
                keysym = self.display.keycode_to_keysym(keycode, 0)
                
                # Release Super key
                if keysym in (XK.XK_Super_L, XK.XK_Super_R):
                    self.super_pressed = False

    def stop(self):
        self.running = False
        if self.display:
            self.display.close()

class ClipboardItem:
    def __init__(self, content, timestamp=None, item_type="text", image_data=None):
        self.content = content
        self.timestamp = timestamp or datetime.now()
        self.type = item_type
        self.image_data = image_data

    def to_dict(self):
        data = {
            "content": self.content,
            "timestamp": self.timestamp.isoformat(),
            "type": self.type
        }
        if self.image_data:
            data["image_data"] = self.image_data
        return data

    @classmethod
    def from_dict(cls, data):
        item = ClipboardItem(data["content"], datetime.fromisoformat(data["timestamp"]), data["type"])
        if "image_data" in data:
            item.image_data = data["image_data"]
        return item

class FluentClip(Gtk.Window):
    def __init__(self):
        Gtk.Window.__init__(self, title="FluentClip")
        self.set_default_size(400, 500)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_border_width(0)
        
        # Initialize notify
        Notify.init("FluentClip")
        
        # Settings
        self.max_history = 30
        self.blur_opacity = 0.85
        self.current_content = ""
        self.history = []
        self.begin_drag = False
        self.is_visible = False
        
        # Setup window properties for blur effect
        self.setup_window_properties()
        
        # Main layout
        self.build_ui()
        
        # Setup styles
        self.load_css()
        
        # Load history
        self.load_history()
        
        # Setup clipboard monitoring
        self.clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        GLib.timeout_add(500, self.check_clipboard)
        
        # Setup global hotkey
        self.setup_hotkey()
        
        # Focus handling for auto-hide
        self.connect("focus-out-event", self.on_focus_out)
        
        # Initialize tray icon
        self.setup_tray_icon()

    def setup_window_properties(self):
        self.set_app_paintable(True)
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual and screen.is_composited():
            self.set_visual(visual)
        
        self.set_decorated(False)
        self.connect("draw", self.on_draw)
        
        # Window dragging
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | 
                       Gdk.EventMask.BUTTON_RELEASE_MASK | 
                       Gdk.EventMask.POINTER_MOTION_MASK)
        self.connect("button-press-event", self.on_window_clicked)
        self.connect("button-release-event", self.on_window_released)
        self.connect("motion-notify-event", self.on_window_motion)
        self.connect("key-press-event", self.on_key_press)

    def build_ui(self):
        # Main container
        self.main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add(self.main_box)
        
        # Header bar
        self.header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.header.set_size_request(-1, 40)
        self.header.get_style_context().add_class("header")
        self.main_box.pack_start(self.header, False, False, 0)
        
        # Title
        title_label = Gtk.Label(label="FluentClip")
        title_label.set_halign(Gtk.Align.START)
        title_label.set_margin_start(15)
        self.header.pack_start(title_label, True, True, 0)
        
        # Control buttons
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_box.set_margin_end(10)
        self.header.pack_end(btn_box, False, False, 0)
        
        # Minimize button
        min_btn = Gtk.Button()
        min_btn.set_relief(Gtk.ReliefStyle.NONE)
        min_btn.set_size_request(30, 30)
        min_btn.connect("clicked", self.on_minimize)
        min_image = Gtk.Image.new_from_icon_name("window-minimize-symbolic", Gtk.IconSize.MENU)
        min_btn.add(min_image)
        btn_box.add(min_btn)
        
        # Close button
        close_btn = Gtk.Button()
        close_btn.set_relief(Gtk.ReliefStyle.NONE)
        close_btn.set_size_request(30, 30)
        close_btn.connect("clicked", self.hide_window)
        close_image = Gtk.Image.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
        close_btn.add(close_image)
        btn_box.add(close_btn)
        
        # Settings button
        settings_btn = Gtk.Button()
        settings_btn.set_relief(Gtk.ReliefStyle.NONE)
        settings_btn.set_size_request(30, 30)
        settings_btn.connect("clicked", self.on_settings)
        settings_image = Gtk.Image.new_from_icon_name("emblem-system-symbolic", Gtk.IconSize.MENU)
        settings_btn.add(settings_image)
        btn_box.add(settings_btn)
        
        # Search box
        search_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        search_box.set_margin_top(8)
        search_box.set_margin_bottom(8)
        search_box.set_margin_start(12)
        search_box.set_margin_end(12)
        self.main_box.pack_start(search_box, False, False, 0)
        
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Search clipboard")
        self.search_entry.connect("search-changed", self.on_search_changed)
        search_box.pack_start(self.search_entry, True, True, 0)
        
        # Clipboard items list
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.main_box.pack_start(scrolled, True, True, 0)
        
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.connect("row-activated", self.on_item_clicked)
        scrolled.add(self.listbox)
        
        # Status bar
        statusbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        statusbar.set_size_request(-1, 30)
        self.main_box.pack_start(statusbar, False, False, 0)
        
        self.status_label = Gtk.Label(label="Items: 0")
        self.status_label.set_margin_start(12)
        statusbar.pack_start(self.status_label, False, False, 0)
        
        # Clear all button
        clear_btn = Gtk.Button(label="Clear All")
        clear_btn.set_margin_end(12)
        clear_btn.connect("clicked", self.on_clear_all)
        statusbar.pack_end(clear_btn, False, False, 0)

    def load_css(self):
        css_provider = Gtk.CssProvider()
        css = f"""
        window {{
            background-color: rgb(30, 30, 30);  /* Dark gray background without transparency */
            border-radius: 12px;
        }}
        
        .header {{
            background-color: rgb(50, 50, 50);  /* Darker header without transparency */
            border-bottom: 1px solid rgba(200, 200, 200, 0.3);
            border-top-left-radius: 12px;
            border-top-right-radius: 12px;
        }}
        
        button {{
            background-color: transparent;
            border: none;
            border-radius: 6px;
            transition: background-color 0.2s;
            color: white;  /* White text for buttons */
        }}
        
        button:hover {{
            background-color: rgb(70, 70, 70);  /* Solid hover color */
        }}
        
        button:active {{
            background-color: rgb(100, 100, 100);  /* Solid active color */
        }}
        
        entry {{
            border-radius: 8px;
            padding: 8px;
            background-color: rgb(50, 50, 50);  /* Dark entry background without transparency */
            border: 1px solid rgba(200, 200, 200, 0.5);
            color: white;  /* White text for entry */
        }}
        
        .clip-item {{
            padding: 8px 12px;
            margin: 4px 8px;
            border-radius: 8px;
            background-color: rgb(40, 40, 40);  /* Dark item background without transparency */
            border: 1px solid rgba(200, 200, 200, 0.5);
            transition: background-color 0.2s;
            color: white;  /* White text for clip items */
        }}
        
        .clip-item:hover {{
            background-color: rgb(60, 60, 60);  /* Solid hover color for clip items */
        }}
        
        .clip-item-selected {{
            background-color: rgb(70, 70, 70);  /* Solid selected color */
            border: 1px solid rgba(0, 120, 215, 0.5);
        }}
        
        .time-label {{
            color: rgb(200, 200, 200);  /* Light gray for timestamps */
            font-size: 11px;
        }}
        
        .image-preview {{
            border-radius: 4px;
            background-color: rgb(60, 60, 60);  /* Solid background for image preview */
        }}
        """
        css_provider.load_from_data(css.encode())
        context = Gtk.StyleContext()
        screen = Gdk.Screen.get_default()
        context.add_provider_for_screen(screen, css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def on_draw(self, widget, cr):
        # Draw background without transparency
        width = widget.get_allocated_width()
        height = widget.get_allocated_height()
        
        # Set solid color for the background
        cr.set_source_rgb(30/255, 30/255, 30/255)  # Dark gray
        cr.rectangle(0, 0, width, height)
        cr.fill()
        
        return False

    def setup_hotkey(self):
        # Try to set up Python-Xlib hotkey (Super+V)
        if XLIB_AVAILABLE:
            self.hotkey_manager = HotKeyManager(self.toggle_window)
            if not self.hotkey_manager.start():
                print("Failed to set up hotkey with python-xlib")
                self.create_shortcut_hint()
        else:
            self.create_shortcut_hint()
            
    def create_shortcut_hint(self):
        # Create shortcut hint dialog
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK,
            text="Hotkey Configuration",
        )
        
        dialog.format_secondary_text(
            "For Super+V hotkey functionality, please install python3-xlib:\n"
            "sudo apt install python3-xlib\n\n"
            "Alternatively, you can set a custom keyboard shortcut in your system settings "
            "to run: 'python3 -c \"import dbus; bus = dbus.SessionBus(); "
            "fluentclip = bus.get_object(\\'org.fluentclip\\', \\'/org/fluentclip\\'); "
            "fluentclip.toggle() if fluentclip else None\"'"
        )
        
        dialog.run()
        dialog.destroy()
        
    def toggle_window(self, key=None):
        if self.is_visible:
            self.hide_window()
        else:
            self.show_window()

    def show_window(self):
        self.show_all()
        self.present()
        self.is_visible = True
        self.grab_focus()

    def hide_window(self, widget=None):
        self.hide()
        self.is_visible = False

    def setup_tray_icon(self):
        # Create tray icon
        try:
            from gi.repository import AppIndicator3 as AppIndicator
            
            self.indicator = AppIndicator.Indicator.new(
                "fluent-clip",
                "edit-paste",
                AppIndicator.IndicatorCategory.APPLICATION_STATUS
            )
            self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
            
            # Create menu
            menu = Gtk.Menu()
            
            show_item = Gtk.MenuItem(label="Open FluentClip")
            show_item.connect("activate", lambda _: self.show_window())
            menu.append(show_item)
            
            separator = Gtk.SeparatorMenuItem()
            menu.append(separator)
            
            quit_item = Gtk.MenuItem(label="Quit")
            quit_item.connect("activate", Gtk.main_quit)
            menu.append(quit_item)
            
            menu.show_all()
            self.indicator.set_menu(menu)
        except ImportError:
            print("AppIndicator not available, tray icon won't be shown")
            # Fallback to standard GTK status icon
            try:
                self.status_icon = Gtk.StatusIcon()
                self.status_icon.set_from_icon_name("edit-paste")
                self.status_icon.set_tooltip_text("FluentClip")
                self.status_icon.connect("activate", lambda _: self.show_window())
                
                # Create right-click menu
                self.status_icon.connect("popup-menu", self.on_status_icon_popup)
            except:
                print("Unable to create status icon, running without system tray")

    def on_status_icon_popup(self, icon, button, time):
        menu = Gtk.Menu()
        
        show_item = Gtk.MenuItem(label="Open FluentClip")
        show_item.connect("activate", lambda _: self.show_window())
        menu.append(show_item)
        
        separator = Gtk.SeparatorMenuItem()
        menu.append(separator)
        
        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", Gtk.main_quit)
        menu.append(quit_item)
        
        menu.show_all()
        menu.popup(None, None, None, None, button, time)

    def on_minimize(self, button):
        self.iconify()
    
    def on_settings(self, button):
        dialog = Gtk.Dialog(title="Settings", parent=self, flags=0)
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                           Gtk.STOCK_OK, Gtk.ResponseType.OK)
        dialog.set_default_size(350, 200)
        
        content_area = dialog.get_content_area()
        
        # Max history items
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        hbox.set_margin_top(12)
        hbox.set_margin_bottom(12)
        hbox.set_margin_start(12)
        hbox.set_margin_end(12)
        label = Gtk.Label(label="Maximum history items:")
        hbox.pack_start(label, False, False, 0)
        
        adjustment = Gtk.Adjustment(value=self.max_history, lower=5, upper=100, step_increment=1)
        spin_button = Gtk.SpinButton()
        spin_button.set_adjustment(adjustment)
        hbox.pack_end(spin_button, False, False, 0)
        content_area.add(hbox)
        
        # Background opacity
        hbox2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        hbox2.set_margin_top(12)
        hbox2.set_margin_bottom(12)
        hbox2.set_margin_start(12)
        hbox2.set_margin_end(12)
        label2 = Gtk.Label(label="Background opacity:")
        hbox2.pack_start(label2, False, False, 0)
        
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0.3, 1.0, 0.05)
        scale.set_value(self.blur_opacity)
        scale.set_draw_value(True)
        scale.set_value_pos(Gtk.PositionType.RIGHT)
        hbox2.pack_end(scale, True, True, 0)
        content_area.add(hbox2)
        
        # Add hotkey settings/info if xlib available
        if XLIB_AVAILABLE:
            hotkey_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            hotkey_box.set_margin_top(12)
            hotkey_box.set_margin_bottom(12)
            hotkey_box.set_margin_start(12)
            hotkey_box.set_margin_end(12)
            hotkey_label = Gtk.Label(label="Hotkey:")
            hotkey_box.pack_start(hotkey_label, False, False, 0)
            
            hotkey_value = Gtk.Label(label="Super+V")
            hotkey_value.set_halign(Gtk.Align.END)
            hotkey_box.pack_end(hotkey_value, False, False, 0)
            content_area.add(hotkey_box)
        
        dialog.show_all()
        response = dialog.run()
        
        if response == Gtk.ResponseType.OK:
            self.max_history = int(spin_button.get_value())
            self.blur_opacity = scale.get_value()
            
            # Update CSS with new opacity
            self.load_css()
            
        dialog.destroy()
    
    def on_search_changed(self, entry):
        search_text = entry.get_text().lower()
        
        # Clear and recreate list based on search
        for child in self.listbox.get_children():
            self.listbox.remove(child)
        
        for item in self.history:
            if item.type == "text" and search_text in item.content.lower():
                self.add_item_to_list(item)
            elif item.type == "image" and search_text in item.content.lower():
                self.add_item_to_list(item)
                
        self.listbox.show_all()
    
    def check_clipboard(self):
        # Check for text
        text = self.clipboard.wait_for_text()
        if text and text != self.current_content and text.strip():
            self.process_clipboard_text(text)
            return True
            
        # Check for image
        pixbuf = self.clipboard.wait_for_image()
        if pixbuf:
            self.process_clipboard_image(pixbuf)
            
        return True
    
    def process_clipboard_text(self, text):
        self.current_content = text
        
        # Check if text already exists in history
        for i, item in enumerate(self.history):
            if item.type == "text" and item.content == text:
                # Move item to top
                self.history.pop(i)
                self.history.insert(0, ClipboardItem(text, datetime.now(), "text"))
                self.refresh_list()
                self.save_history()
                return
        
        # Add new item
        new_item = ClipboardItem(text, datetime.now(), "text")
        self.history.insert(0, new_item)
        
        # Limit history size
        if len(self.history) > self.max_history:
            self.history = self.history[:self.max_history]
        
        self.refresh_list()
        self.save_history()
    
    def process_clipboard_image(self, pixbuf):
        # Convert pixbuf to base64 data
        success, data = pixbuf.save_to_bufferv("png", [], [])
        if success:
            image_data = base64.b64encode(data).decode('utf-8')
            
            # Check if image already exists
            for i, item in enumerate(self.history):
                if item.type == "image" and item.image_data == image_data:
                    # Move item to top
                    self.history.pop(i)
                    timestamp = datetime.now()
                    self.history.insert(0, ClipboardItem(f"Image {timestamp.strftime('%Y-%m-%d %H:%M:%S')}", 
                                                        timestamp, "image", image_data))
                    self.refresh_list()
                    self.save_history()
                    return
            
            # Add new image item
            timestamp = datetime.now()
            new_item = ClipboardItem(f"Image {timestamp.strftime('%Y-%m-%d %H:%M:%S')}", 
                                    timestamp, "image", image_data)
            self.history.insert(0, new_item)
            
            # Limit history size
            if len(self.history) > self.max_history:
                self.history = self.history[:self.max_history]
            
            self.refresh_list()
            self.save_history()
    
    def refresh_list(self):
        # Update entire list
        for child in self.listbox.get_children():
            self.listbox.remove(child)
        
        for item in self.history:
            self.add_item_to_list(item)
        
        self.listbox.show_all()
        self.status_label.set_text(f"Items: {len(self.history)}")
    
    def add_item_to_list(self, item):
        # Create widget for list item
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(2)
        box.set_margin_bottom(2)
        box.get_style_context().add_class("clip-item")
        
        if item.type == "text":
            # Text content
            content = item.content
            if len(content) > 100:
                content = content[:97] + "..."
            
            label = Gtk.Label(label=content)
            label.set_line_wrap(True)
            label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            label.set_xalign(0)
            label.set_max_width_chars(50)
            box.pack_start(label, True, True, 0)
        elif item.type == "image":
            # Image content
            try:
                image_data = base64.b64decode(item.image_data)
                loader = GdkPixbuf.PixbufLoader()
                loader.write(image_data)
                loader.close()
                pixbuf = loader.get_pixbuf()
                
                # Scale image preview
                width = pixbuf.get_width()
                height = pixbuf.get_height()
                
                if width > 300:
                    scale_factor = 300 / width
                    new_width = 300
                    new_height = int(height * scale_factor)
                    pixbuf = pixbuf.scale_simple(new_width, new_height, GdkPixbuf.InterpType.BILINEAR)
                
                image = Gtk.Image.new_from_pixbuf(pixbuf)
                image.get_style_context().add_class("image-preview")
                box.pack_start(image, True, True, 0)
                
                # Add image label
                img_label = Gtk.Label(label="Image")
                img_label.set_xalign(0)
                box.pack_start(img_label, False, False, 0)
            except Exception as e:
                print(f"Error loading image preview: {e}")
                label = Gtk.Label(label="[Image - preview unavailable]")
                label.set_xalign(0)
                box.pack_start(label, True, True, 0)
        
        # Timestamp
        time_str = item.timestamp.strftime("%d/%m/%Y %H:%M")
        time_label = Gtk.Label(label=time_str)
        time_label.get_style_context().add_class("time-label")
        time_label.set_xalign(0)
        box.pack_start(time_label, False, False, 0)
        
        # Add to listbox
        row = Gtk.ListBoxRow()
        row.add(box)
        row.item = item  # Store item reference
        self.listbox.add(row)
    
    def on_item_clicked(self, listbox, row):
        if row and hasattr(row, 'item'):
            item = row.item
            
            if item.type == "text":
                # Copy text to clipboard
                self.clipboard.set_text(item.content, -1)
            elif item.type == "image":
                # Copy image to clipboard
                try:
                    image_data = base64.b64decode(item.image_data)
                    loader = GdkPixbuf.PixbufLoader()
                    loader.write(image_data)
                    loader.close()
                    pixbuf = loader.get_pixbuf()
                    self.clipboard.set_image(pixbuf)
                except Exception as e:
                    print(f"Error setting image to clipboard: {e}")
                    
            # Show notification
            notification = Notify.Notification.new(
                "Copied to clipboard",
                item.content[:50] + ("..." if len(item.content) > 50 else ""),
                "edit-copy"
            )
            notification.show()
            
            # Hide window after selection
            self.hide_window()
    
    def on_clear_all(self, button):
        # Confirm dialog
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text="Clear entire clipboard history?",
        )
        
        response = dialog.run()
        dialog.destroy()
        
        if response == Gtk.ResponseType.YES:
            self.history = []
            self.refresh_list()
            self.save_history()
    
    def load_history(self):
        # Load history from file
        config_dir = os.path.join(GLib.get_user_config_dir(), "fluentclip")
        os.makedirs(config_dir, exist_ok=True)
        history_file = os.path.join(config_dir, "history.json")
        
        if os.path.exists(history_file):
            try:
                with open(history_file, 'r') as f:
                    data = json.load(f)
                    
                    if isinstance(data, list):
                        self.history = [ClipboardItem.from_dict(item) for item in data]
                        self.refresh_list()
                        
                        if self.history:
                            self.current_content = self.history[0].content
            except Exception as e:
                print(f"Error loading history: {e}")
    
    def save_history(self):
        # Save history to file
        config_dir = os.path.join(GLib.get_user_config_dir(), "fluentclip")
        os.makedirs(config_dir, exist_ok=True)
        history_file = os.path.join(config_dir, "history.json")
        
        try:
            data = [item.to_dict() for item in self.history]
            with open(history_file, 'w') as f:
                json.dump(data, f)
        except Exception as e:
            print(f"Error saving history: {e}")
    
    def on_window_clicked(self, widget, event):
        # Start window drag
        if event.button == 1 and event.y < 40:  # Left click on header
            self.begin_drag = True
            self.drag_x, self.drag_y = event.x_root, event.y_root
            self.window_x, self.window_y = self.get_position()
            return True
        return False
    
    def on_window_released(self, widget, event):
        # End window drag
        self.begin_drag = False
        return True
    
    def on_window_motion(self, widget, event):
        # Move window when dragging
        if self.begin_drag:
            dx = event.x_root - self.drag_x
            dy = event.y_root - self.drag_y
            new_x = self.window_x + dx
            new_y = self.window_y + dy
            self.move(int(new_x), int(new_y))
        return True
    
    def on_focus_out(self, widget, event):
        # Auto hide when focus is lost
        self.hide_window()
        return True
    
    def on_key_press(self, widget, event):
        # Escape key to close
        if event.keyval == Gdk.KEY_Escape:
            self.hide_window()
            return True
        return False

# DBus service for remote activation
class FluentClipService(dbus.service.Object):
    def __init__(self, app):
        bus_name = dbus.service.BusName('org.fluentclip', bus=dbus.SessionBus())
        dbus.service.Object.__init__(self, bus_name, '/org/fluentclip')
        self.app = app
    
    @dbus.service.method('org.fluentclip')
    def toggle(self):
        self.app.toggle_window()
        return True

def setup_blur(window):
    """Setup blur effect using available compositors"""
    # Try to detect compositor type and apply blur
    try:
        # KDE KWin blur
        if os.environ.get('XDG_CURRENT_DESKTOP') == 'KDE':
            window.connect("realize", lambda w: setup_kde_blur(w))
        # GNOME blur (via extension)
        elif os.environ.get('XDG_CURRENT_DESKTOP') == 'GNOME':
            window.connect("realize", lambda w: setup_gnome_blur(w))
        # Cinnamon blur (Muffin)
        elif os.environ.get('XDG_CURRENT_DESKTOP') == 'Cinnamon':
            window.connect("realize", lambda w: setup_cinnamon_blur(w))
        # Attempt blur on other compositors
        else:
            window.connect("realize", lambda w: try_generic_blur(w))
    except Exception as e:
        print(f"Couldn't apply blur effect: {e}")

def setup_cinnamon_blur(window):
    """Setup blur for Cinnamon (Muffin)"""
    try:
        gdk_window = window.get_window()
        if gdk_window:
            # Set the window type hint for Muffin
            gdk_window.set_type_hint(Gdk.WindowTypeHint.NORMAL)
            # Set the opacity for the window
            gdk_window.set_opacity(0.85)  # Adjust this value as needed
            # Set the window to be transparent
            gdk_window.set_background_rgba(Gdk.RGBA(0, 0, 0, 0))  # Fully transparent background
        else:
            print("Failed to get GDK window for Cinnamon blur")
    except Exception as e:
        print(f"Cinnamon blur failed: {e}")

def setup_kde_blur(window):
    """Setup blur for KDE KWin"""
    try:
        gdk_window = window.get_window()
        if gdk_window:
            gdk_window.set_type_hint(Gdk.WindowTypeHint.NORMAL)
            gdk_window.set_opacity(0.85)  # Adjust this value as needed
        else:
            print("Failed to get GDK window for KDE blur")
    except Exception as e:
        print(f"KDE blur failed: {e}")

def setup_gnome_blur(window):
    """Setup blur for GNOME (requires blur-my-shell extension)"""
    try:
        gdk_window = window.get_window()
        if gdk_window:
            gdk_window.set_type_hint(Gdk.WindowTypeHint.NORMAL)
            gdk_window.set_opacity(0.85)  # Adjust this value as needed
        else:
            print("Failed to get GDK window for GNOME blur")
    except Exception as e:
        print(f"GNOME blur failed: {e}")

def try_generic_blur(window):
    """Try to apply blur using Picom or Compton"""
    try:
        gdk_window = window.get_window()
        if gdk_window:
            gdk_window.set_type_hint(Gdk.WindowTypeHint.NORMAL)
            gdk_window.set_opacity(0.85)  # Adjust this value as needed
        else:
            print("Failed to get GDK window for generic blur")
    except Exception as e:
        print(f"Generic blur failed: {e}")

def set_window_properties(window):
    """Set the opacity and WM_CLASS of the window"""
    gdk_window = window.get_window()
    if gdk_window:
        # Set opacity
        gdk_window.set_opacity(0.85)
        
        # Set window type hint
        gdk_window.set_type_hint(Gdk.WindowTypeHint.NORMAL)
    else:
        print("Failed to get GDK window for setting properties")

def setup_window_properties(self):
    self.set_app_paintable(True)
    screen = self.get_screen()
    visual = screen.get_rgba_visual()
    if visual and screen.is_composited():
        self.set_visual(visual)
    
    self.set_decorated(False)
    self.connect("draw", self.on_draw)
    
    # Set the window opacity to fully opaque
    gdk_window = self.get_window()
    if gdk_window:
        gdk_window.set_opacity(1.0)  # Fully opaque

    # Window dragging
    self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | 
                   Gdk.EventMask.BUTTON_RELEASE_MASK | 
                   Gdk.EventMask.POINTER_MOTION_MASK)
    self.connect("button-press-event", self.on_window_clicked)
    self.connect("button-release-event", self.on_window_released)
    self.connect("motion-notify-event", self.on_window_motion)
    self.connect("key-press-event", self.on_key_press)

def single_instance_check():
    """Check if application is already running"""
    bus = dbus.SessionBus()
    try:
        # Check if service is already running
        existing = bus.get_object('org.fluentclip', '/org/fluentclip')
        if existing:
            iface = dbus.Interface(existing, 'org.fluentclip')
            iface.toggle()
            print("FluentClip is already running, toggling existing instance")
            return False
    except dbus.DBusException:
        # Not running, continue
        return True

def main():
    # Initialize DBus mainloop
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    
    # Check for single instance
    if not single_instance_check():
        return
    
    # Create application
    app = FluentClip()
    
    # Apply blur effect
    setup_blur(app)
    
    # Start DBus service
    service = FluentClipService(app)
    
    # Set up signal handling
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    
    # Show initially
    app.show_all()
    
    # Run main loop
    Gtk.main()

if __name__ == "__main__":
    main()