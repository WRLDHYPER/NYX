// NyxLauncher.cs - source for NYX.exe, a tiny launcher for the Nyx desktop app.
// It starts nyx_app.py with pythonw (no console window), the same way
// run_app.bat does, and carries the NYX icon. Build it with tools\build_exe.bat.
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows.Forms;

[assembly: AssemblyTitle("NYX")]
[assembly: AssemblyProduct("NYX voice assistant")]
[assembly: AssemblyDescription("NYX voice assistant")]
[assembly: AssemblyVersion("1.0.0.0")]
[assembly: AssemblyFileVersion("1.0.0.0")]

static class NyxLauncher
{
    [STAThread]
    static int Main()
    {
        string dir = AppDomain.CurrentDomain.BaseDirectory;
        string script = Path.Combine(dir, "nyx_app.py");
        if (!File.Exists(script))
        {
            Fail("Can't find nyx_app.py next to NYX.exe.\n\nKeep NYX.exe in the Nyx folder:\n" + dir);
            return 1;
        }

        try { Shortcuts.Stamp(Path.Combine(dir, "NYX.exe")); }
        catch (Exception) { }   // never let this stop Nyx from starting

        string venvPyw = Path.Combine(dir, @".venv\Scripts\pythonw.exe");
        string quoted = "\"" + script + "\"";

        // 1) the environment made by setup.bat, 2) the py launcher, 3) pythonw on PATH
        if (File.Exists(venvPyw) && Start(venvPyw, quoted, dir)) return 0;
        if (Start("pyw", "-3.11 " + quoted, dir)) return 0;
        if (Start("pythonw", quoted, dir)) return 0;

        Fail("Couldn't find Python 3.11 to run Nyx.\n\nRun setup.bat first to install everything Nyx needs.");
        return 1;
    }

    static bool Start(string exe, string args, string workDir)
    {
        try
        {
            var psi = new ProcessStartInfo(exe, args);
            psi.WorkingDirectory = workDir;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            Process.Start(psi);
            return true;
        }
        catch (Exception)
        {
            return false;
        }
    }

    static void Fail(string message)
    {
        MessageBox.Show(message, "NYX", MessageBoxButtons.OK, MessageBoxIcon.Error);
    }
}

// The app window (pythonw) groups itself on the taskbar under AppId. A pin or
// shortcut made from NYX.exe has no ID, so Windows would show it as a separate
// button. This gives every NYX.exe shortcut the same ID so they line up.
static class Shortcuts
{
    public const string AppId = "Nyx.Assistant";   // must match APP_ID in nyx_app.py

    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    struct PropertyKey
    {
        public Guid fmtid; public uint pid;
        public PropertyKey(Guid g, uint p) { fmtid = g; pid = p; }
    }

    [StructLayout(LayoutKind.Sequential)]
    struct PropVariant
    {
        public ushort vt; ushort r1, r2, r3; public IntPtr p; IntPtr p2;
    }

    [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IPropertyStore
    {
        void GetCount(out uint count);
        void GetAt(uint index, out PropertyKey key);
        void GetValue(ref PropertyKey key, out PropVariant value);
        void SetValue(ref PropertyKey key, ref PropVariant value);
        void Commit();
    }

    [ComImport, Guid("000214F9-0000-0000-C000-000000000046"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IShellLinkW
    {
        void GetPath([Out, MarshalAs(UnmanagedType.LPWStr)] StringBuilder file, int max,
                     IntPtr findData, uint flags);
        // (remaining methods not needed)
    }

    [ComImport, Guid("0000010B-0000-0000-C000-000000000046"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IPersistFile
    {
        void GetClassID(out Guid clsid);
        [PreserveSig] int IsDirty();
        void Load([MarshalAs(UnmanagedType.LPWStr)] string file, uint mode);
        void Save([MarshalAs(UnmanagedType.LPWStr)] string file, bool remember);
        void SaveCompleted([MarshalAs(UnmanagedType.LPWStr)] string file);
        void GetCurFile([MarshalAs(UnmanagedType.LPWStr)] out string file);
    }

    [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
    class ShellLink { }

    [DllImport("ole32.dll")]
    static extern int PropVariantClear(ref PropVariant pv);

    static readonly PropertyKey KeyAppId =
        new PropertyKey(new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), 5);

    public static void Stamp(string exePath)
    {
        string appData = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
        string[] folders = {
            Path.Combine(appData, @"Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar"),
            Path.Combine(appData, @"Microsoft\Internet Explorer\Quick Launch"),
            Environment.GetFolderPath(Environment.SpecialFolder.Programs),
            Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),
        };
        foreach (string folder in folders)
        {
            if (!Directory.Exists(folder)) continue;
            foreach (string lnk in Directory.GetFiles(folder, "*.lnk"))
            {
                try { StampOne(lnk, exePath); } catch (Exception) { }
            }
        }
    }

    static void StampOne(string lnk, string exePath)
    {
        object link = new ShellLink();
        try
        {
            var file = (IPersistFile)link;
            file.Load(lnk, 2 /* STGM_READWRITE */);
            var target = new StringBuilder(1024);
            ((IShellLinkW)link).GetPath(target, target.Capacity, IntPtr.Zero, 4 /* SLGP_RAWPATH */);
            string resolved = Environment.ExpandEnvironmentVariables(target.ToString());
            if (resolved.Length == 0 || resolved.StartsWith(@"\\")) return;   // not a local file
            if (!string.Equals(Path.GetFullPath(resolved), exePath, StringComparison.OrdinalIgnoreCase))
                return;
            var store = (IPropertyStore)link;
            if (ReadString(store, KeyAppId) == AppId)
                return;
            var pv = new PropVariant();
            pv.vt = 31;   // VT_LPWSTR
            pv.p = Marshal.StringToCoTaskMemUni(AppId);
            PropertyKey key = KeyAppId;
            try { store.SetValue(ref key, ref pv); store.Commit(); }
            finally { PropVariantClear(ref pv); }
            file.Save(null, true);
        }
        finally { Marshal.ReleaseComObject(link); }
    }

    static string ReadString(IPropertyStore store, PropertyKey key)
    {
        PropVariant pv;
        store.GetValue(ref key, out pv);
        try { return pv.vt == 31 ? Marshal.PtrToStringUni(pv.p) : null; }
        finally { PropVariantClear(ref pv); }
    }
}
