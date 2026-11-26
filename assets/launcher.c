/* MicMix.app's program, for macOS. It runs MicMix (micmix.pyw in ~/Library/Application Support/MicMix) on the
   Python that install.sh set up there, inside this process. So macOS sees one app called MicMix: its icon in the
   Dock, its name in the menu bar, and its own permissions for the mic and for recording other apps' sound (a Python
   started from here would be a second app, asking as "Python"). It doesn't change when MicMix updates, so those
   permissions stay.

   install.sh ships it built (assets/MicMix-launcher). To build it again, on a Mac with Xcode's command line tools:
   clang -arch arm64 -arch x86_64 -mmacosx-version-min=11.0 -Os -Wall -o assets/MicMix-launcher assets/launcher.c \
         -framework CoreFoundation
*/
#include <CoreFoundation/CoreFoundation.h>
#include <dlfcn.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int fail(const char *why) {
    CFStringRef text = CFStringCreateWithCString(NULL, why, kCFStringEncodingUTF8);
    CFUserNotificationDisplayAlert(0, kCFUserNotificationStopAlertLevel, NULL, NULL, NULL,
                                   CFSTR("MicMix couldn't start"), text, NULL, NULL, NULL, NULL);
    CFRelease(text);
    return 1;
}

int main(int argc, char **argv) {
    char dir[PATH_MAX], path[PATH_MAX], lib[PATH_MAX], home[PATH_MAX];
    const char *user = getenv("HOME");
    (void)argc;
    snprintf(dir, sizeof dir, "%s/Library/Application Support/MicMix", user ? user : "");
    snprintf(path, sizeof path, "%s/python.txt", dir);  /* the Python's library, then its home: install.sh writes it */
    FILE *file = fopen(path, "r");
    if (!file)
        return fail("MicMix's files aren't there. Run its installer again (see README.md).");
    int whole = fgets(lib, sizeof lib, file) && fgets(home, sizeof home, file);
    fclose(file);
    if (!whole)
        return fail("MicMix's python.txt is cut short. Run its installer again.");
    lib[strcspn(lib, "\r\n")] = 0;
    home[strcspn(home, "\r\n")] = 0;
    setenv("PYTHONHOME", home, 1);
    void *python = dlopen(lib, RTLD_NOW | RTLD_GLOBAL);
    int (*run)(int, char **) = python ? (int (*)(int, char **))dlsym(python, "Py_BytesMain") : NULL;
    if (!run)
        return fail("MicMix's Python is missing. Run its installer again.");
    snprintf(path, sizeof path, "%s/micmix.pyw", dir);
    char *args[] = {argv[0], path, NULL};
    return run(2, args);
}
