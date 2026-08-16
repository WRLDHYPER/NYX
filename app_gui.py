"""
gui_app.py - Tkinter GUI for Nyx/Nix voice assistant.
Displays transcript, replies, status, and provides start/stop controls.
"""
import tkinter as tk
from tkinter import scrolledtext, ttk
import threading
import queue
import sys
import time

# Import the core assistant class
from app_code import VoiceAssistant

class AssistantGUI:
    def __init__(self, root):
        self.root = root
        root.title("Nyx / Nix Voice Assistant")
        root.geometry("600x500")
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        # Variables
        self.assistant = None
        self.thread = None
        self.running = False
        self.event_queue = queue.Queue(maxsize=100)

        # Build UI
        self.create_widgets()

        # Start the assistant thread automatically?
        # We'll let the user click "Start" to begin.
        self.status_var.set("Ready. Click 'Start' to launch assistant.")

    def create_widgets(self):
        # Status bar
        self.status_var = tk.StringVar(value="Idle")
        status_label = ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
        status_label.pack(fill=tk.X, padx=5, pady=5)

        # Main display: conversation log
        self.log_text = scrolledtext.ScrolledText(self.root, wrap=tk.WORD, height=20)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self.log_text.config(state=tk.DISABLED)

        # Control frame
        control_frame = ttk.Frame(self.root)
        control_frame.pack(fill=tk.X, padx=5, pady=5)

        self.start_btn = ttk.Button(control_frame, text="Start Assistant", command=self.start_assistant)
        self.start_btn.pack(side=tk.LEFT, padx=5)

        self.stop_btn = ttk.Button(control_frame, text="Stop Assistant", command=self.stop_assistant, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=5)

        quit_btn = ttk.Button(control_frame, text="Quit", command=self.on_close)
        quit_btn.pack(side=tk.RIGHT, padx=5)

        # Add a small text entry for manual commands (optional)
        ttk.Label(control_frame, text="Manual command:").pack(side=tk.LEFT, padx=(20,5))
        self.cmd_entry = ttk.Entry(control_frame, width=20)
        self.cmd_entry.pack(side=tk.LEFT, padx=5)
        self.cmd_entry.bind("<Return>", self.send_manual_command)
        send_btn = ttk.Button(control_frame, text="Send", command=self.send_manual_command)
        send_btn.pack(side=tk.LEFT, padx=5)

    def start_assistant(self):
        if self.running:
            return
        self.running = True
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.status_var.set("Starting assistant...")
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, "\n--- Starting assistant ---\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

        # Create assistant instance (this initialises audio, so do it in thread)
        self.assistant = VoiceAssistant()
        # Use a queue to receive events
        self.event_queue = queue.Queue(maxsize=100)

        # Start the run loop in a background thread
        self.thread = threading.Thread(target=self._run_assistant, daemon=True)
        self.thread.start()

        # Start polling the queue
        self.poll_queue()

    def _run_assistant(self):
        """Target for the background thread."""
        try:
            self.assistant.run(event_queue=self.event_queue)
        except Exception as e:
            # Put error into queue so UI can display it
            self.event_queue.put_nowait({"type": "error", "text": f"Assistant crashed: {e}"})
        finally:
            # Ensure UI knows we stopped
            self.event_queue.put_nowait({"type": "status", "text": "Assistant stopped."})

    def poll_queue(self):
        """Periodically check the event queue and update UI."""
        try:
            while True:
                event = self.event_queue.get_nowait()
                self.handle_event(event)
        except queue.Empty:
            pass
        finally:
            # Continue polling if assistant is running
            if self.running:
                self.root.after(100, self.poll_queue)

    def handle_event(self, event):
        """Process an event from the assistant."""
        etype = event.get("type")
        text = event.get("text", "")
        if etype == "transcript":
            self.append_log(f"[You] {text}")
        elif etype == "reply":
            self.append_log(f"[Nyx] {text}")
        elif etype == "status":
            self.status_var.set(text)
            # Also log if not too noisy
            if text.startswith("Listening") or "energy" in text or "silence" in text:
                pass  # skip verbose status
            else:
                self.append_log(f"[Status] {text}")
        elif etype == "error":
            self.append_log(f"[ERROR] {text}")
            self.status_var.set(f"Error: {text}")
        else:
            self.append_log(f"[Unknown] {text}")

        # If the assistant stopped (status "Assistant stopped."), update buttons
        if etype == "status" and text == "Assistant stopped.":
            self.running = False
            self.start_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)
            if self.assistant:
                self.assistant.close()
                self.assistant = None

    def append_log(self, text):
        """Insert text into the log widget and scroll."""
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, text + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def stop_assistant(self):
        """Stop the assistant gracefully."""
        if not self.running:
            return
        self.status_var.set("Stopping assistant...")
        if self.assistant:
            self.assistant.stop()
        # The thread will exit when the loop stops, and we'll get a status event.
        # But we also set buttons immediately to prevent double-click.
        self.stop_btn.config(state=tk.DISABLED)

    def send_manual_command(self, event=None):
        """Send a text command directly to the assistant (for testing)."""
        cmd = self.cmd_entry.get().strip()
        if not cmd:
            return
        self.cmd_entry.delete(0, tk.END)
        # We need to simulate a command – we can push a fake event or call methods.
        # Since the assistant is in a separate thread, we'd need to queue it.
        # For simplicity, we'll just log it and let the user use voice.
        self.append_log(f"[Manual] {cmd}")
        # You could extend this to send the command to the assistant via a queue.

    def on_close(self):
        """Clean up and exit."""
        if self.running:
            self.stop_assistant()
            # Give thread a moment to finish
            time.sleep(0.5)
        if self.assistant:
            self.assistant.close()
        self.root.destroy()
        sys.exit(0)

if __name__ == "__main__":
    root = tk.Tk()
    app = AssistantGUI(root)
    root.mainloop()