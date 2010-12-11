# -*- coding: utf-8 -*-
# inotify.py: inotify-based event emitter for Linux 2.6.13+.
#
# Copyright (C) 2010 Luke McCarthy <luke@iogopro.co.uk>
# Copyright (C) 2010 Gora Khargosh <gora.khargosh@gmail.com>
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

"""
:module: watchdog.observers.inotify
:synopsis: ``inotify(7)`` based emitter implementation.
:author: Luke McCarthy <luke@iogopro.co.uk>
:author: Gora Khargosh <gora.khargosh@gmail.com>
:platforms: Linux 2.6.13+.

.. ADMONITION:: About system requirements

    Recommended minimum kernel version: 2.6.25.

    Quote from the inotify(7) man page:

        "Inotify was merged into the 2.6.13 Linux kernel. The required library
        interfaces were added to glibc in version 2.4. (IN_DONT_FOLLOW,
        IN_MASK_ADD, and IN_ONLYDIR were only added in version 2.5.)"

    Therefore, you must ensure the system is running at least these versions
    appropriate libraries and the kernel.

.. ADMONITION:: About recursiveness, event order, and event coalescing

    Quote from the inotify(7) man page:

        If successive output inotify events produced on the inotify file
        descriptor are identical (same wd, mask, cookie, and name) then they
        are coalesced into a single event if the older event has not yet been
        read (but see BUGS).

        The events returned by reading from an inotify file descriptor form
        an ordered queue. Thus, for example, it is guaranteed that when
        renaming from one directory to another, events will be produced in
        the correct order on the inotify file descriptor.

        ...

        Inotify monitoring of directories is not recursive: to monitor
        subdirectories under a directory, additional watches must be created.

    This emitter implementation therefore automatically adds watches for
    sub-directories if running in recursive mode.

Some extremely useful articles and documentation:

.. _inotify FAQ: http://inotify.aiken.cz/?section=inotify&page=faq&lang=en
.. _intro to inotify: http://www.linuxjournal.com/article/8478

"""

from __future__ import with_statement
from watchdog.utils import platform

if platform.is_linux():
    import os
    import struct
    import threading

    from ctypes import \
        CDLL, \
        CFUNCTYPE, \
        POINTER, \
        c_int, \
        c_char_p, \
        c_uint32, \
        get_errno, \
        sizeof, \
        Structure
    from watchdog.utils import absolute_path
    from watchdog.observers.api import \
        EventEmitter, \
        BaseObserver, \
        DEFAULT_EMITTER_TIMEOUT, \
        DEFAULT_OBSERVER_TIMEOUT


    libc = CDLL('libc.so.6')

    # #include <sys/inotify.h>
    # char *strerror(int errnum);
    strerror = CFUNCTYPE(c_char_p, c_int)(
        ("strerror", libc))

    # #include <sys/inotify.h>
    # int inotify_init(void);
    inotify_init = CFUNCTYPE(c_int, use_errno=True)(
        ("inotify_init", libc))

    # #include <sys/inotify.h>
    # int inotify_init1(int flags);
    inotify_init1 = CFUNCTYPE(c_int, c_int, use_errno=True)(
        ("inotify_init1", libc))

    # #include <sys/inotify.h>
    # int inotify_add_watch(int fd, const char *pathname, uint32_t mask);
    inotify_add_watch = \
        CFUNCTYPE(c_int, c_int, c_char_p, c_uint32, use_errno=True)(
            ("inotify_add_watch", libc))

    # #include <sys/inotify.h>
    # int inotify_rm_watch(int fd, uint32_t wd);
    inotify_rm_watch = CFUNCTYPE(c_int, c_int, c_uint32, use_errno=True)(
        ("inotify_rm_watch", libc))


    class InotifyEvent(object):
        """
        Inotify event struct wrapper.

        :param wd:
            Watch descriptor
        :param mask:
            Event mask
        :param cookie:
            Event cookie
        :param name:
            Event name.
        """
        def __init__(self, wd, mask, cookie, name):
            self._wd = wd
            self._mask = mask
            self._cookie = cookie
            self._name = name

        @property
        def wd(self):
            return self._wd

        @property
        def mask(self):
            return self._mask

        @property
        def cookie(self):
            return self._cookie

        @property
        def name(self):
            return self._name

        # Test event types.
        @property
        def is_modify(self):
            return self._mask & Inotify.IN_MODIFY

        @property
        def is_close_write(self):
            return self._mask & Inotify.IN_CLOSE_WRITE

        @property
        def is_close_nowrite(self):
            return self._mask & Inotify.IN_CLOSE_NOWRITE

        @property
        def is_access(self):
            return self._mask & Inotify.IN_ACCESS

        @property
        def is_delete(self):
            return self._mask & Inotify.IN_DELETE

        @property
        def is_create(self):
            return self._mask & Inotify.IN_CREATE

        @property
        def is_moved_from(self):
            return self._mask & Inotify.IN_MOVED_FROM

        @property
        def is_moved_to(self):
            return self._mask & Inotify.IN_MOVED_TO

        @property
        def is_move(self):
            return self._mask & Inotify.IN_MOVE

        @property
        def is_attrib(self):
            return self._mask & Inotify.IN_ATTRIB

        @property
        def is_directory(self):
            return self._mask & Inotify.IN_ISDIR

        # Additional functionality.
        @property
        def key(self):
            return (self._wd, self._mask, self._cookie, self._name)

        def __eq__(self, inotify_event):
            return self.key == inotify_event.key

        def __ne__(self, inotify_event):
            return self.key == inotify_event.key

        def __hash__(self):
            return hash(self.key)


    class inotify_event_struct(Structure):
        """
        Structure representation of the inotify_event structure.
        Used in buffer size calculations::

            struct inotify_event {
                __s32 wd;            /* watch descriptor */
                __u32 mask;          /* watch mask */
                __u32 cookie;        /* cookie to synchronize two events */
                __u32 len;           /* length (including nulls) of name */
                char  name[0];       /* stub for possible name */
            };
        """
        _fields_ = [('wd',     c_int),
                    ('mask',   c_uint32),
                    ('cookie', c_uint32),
                    ('len',    c_uint32),
                    ('name',   c_char_p)]

    EVENT_SIZE = sizeof(inotify_event_struct)
    DEFAULT_EVENT_BUFFER_SIZE = 1024 * (EVENT_SIZE + 16)


    class Inotify(object):
        """
        Linux inotify(7) API wrapper class.

        :param path:
            The directory path for which we want an inotify object.
        :param recursive:
            ``True`` if subdirectories should be monitored; ``False`` otherwise.
        :param non_blocking:
            ``True`` to initialize inotify in non-blocking mode; ``False``
            otherwise.
        """
        # User-space events
        IN_ACCESS        = 0x00000001     # File was accessed.
        IN_MODIFY        = 0x00000002     # File was modified.
        IN_ATTRIB        = 0x00000004     # Meta-data changed.
        IN_CLOSE_WRITE   = 0x00000008     # Writable file was closed.
        IN_CLOSE_NOWRITE = 0x00000010     # Unwritable file closed.
        IN_OPEN          = 0x00000020     # File was opened.
        IN_MOVED_FROM    = 0x00000040     # File was moved from X.
        IN_MOVED_TO      = 0x00000080     # File was moved to Y.
        IN_CREATE        = 0x00000100     # Subfile was created.
        IN_DELETE        = 0x00000200     # Subfile was deleted.
        IN_DELETE_SELF   = 0x00000400     # Self was deleted.
        IN_MOVE_SELF     = 0x00000800     # Self was moved.

        # Helper user-space events.
        IN_CLOSE         = IN_CLOSE_WRITE | IN_CLOSE_NOWRITE  # Close.
        IN_MOVE          = IN_MOVED_FROM | IN_MOVED_TO  # Moves.

        # Events sent by the kernel to a watch.
        IN_UNMOUNT       = 0x00002000     # Backing file system was unmounted.
        IN_Q_OVERFLOW    = 0x00004000     # Event queued overflowed.
        IN_IGNORED       = 0x00008000     # File was ignored.

        # Special flags.
        IN_ONLYDIR       = 0x01000000     # Only watch the path if it's a directory.
        IN_DONT_FOLLOW   = 0x02000000     # Do not follow a symbolic link.
        IN_EXCL_UNLINK   = 0x04000000     # Exclude events on unlinked objects
        IN_MASK_ADD      = 0x20000000     # Add to the mask of an existing watch.
        IN_ISDIR         = 0x40000000     # Event occurred against directory.
        IN_ONESHOT       = 0x80000000     # Only send event once.

        # All user-space events.
        IN_ALL_EVENTS = reduce(lambda x, y: x | y, [
            IN_ACCESS,
            IN_MODIFY,
            IN_ATTRIB,
            IN_CLOSE_WRITE,
            IN_CLOSE_NOWRITE,
            IN_OPEN,
            IN_MOVED_FROM,
            IN_MOVED_TO,
            IN_DELETE,
            IN_CREATE,
            IN_DELETE_SELF,
            IN_MOVE_SELF,
        ])

        # Flags for ``inotify_init1``
        IN_CLOEXEC = 0x02000000
        IN_NONBLOCK = 0x00004000

        # All inotify bits.
        ALL_INOTIFY_BITS = reduce(lambda x, y: x | y, [
            IN_ACCESS,
            IN_MODIFY,
            IN_ATTRIB,
            IN_CLOSE_WRITE,
            IN_CLOSE_NOWRITE,
            IN_OPEN,
            IN_MOVED_FROM,
            IN_MOVED_TO,
            IN_CREATE,
            IN_DELETE,
            IN_DELETE_SELF,
            IN_MOVE_SELF,
            IN_UNMOUNT,
            IN_Q_OVERFLOW,
            IN_IGNORED,
            IN_ONLYDIR,
            IN_DONT_FOLLOW,
            IN_EXCL_UNLINK,
            IN_MASK_ADD,
            IN_ISDIR,
            IN_ONESHOT,
        ])

        def __init__(self,
                     path,
                     recursive=False,
                     event_mask=Inotify.IN_ALL_EVENTS,
                     non_blocking=False):
            # The file descriptor associated with the inotify instance.
            if non_blocking:
                inotify_fd = inotify_init1(Inotify.IN_NONBLOCK)
            else:
                inotify_fd = inotify_init()
            if inotify_fd == -1:
                Inotify._raise_error()
            self._inotify_fd = inotify_fd
            self._lock = threading.Lock()

            # Stores the watch descriptor for a given path.
            self._wd_for_path = dict()

            path = absolute_path(path)
            self._path = path
            self._event_mask = event_mask
            self._is_recursive = recursive
            self._is_non_blocking = non_blocking
            self._add_dir_watch(path, recursive, event_mask)

        @property
        def event_mask(self):
            """The event mask for this inotify instance."""
            return self._event_mask

        @property
        def path(self):
            """The path associated with the inotify instance."""
            return self._path

        @property
        def is_recursive(self):
            """Whether we are watching directories recursively."""
            return self._is_recursive

        @property
        def is_non_blocking(self):
            """Determines whether this instance of inotify is non-blocking."""
            return self._is_non_blocking

        @property
        def fd(self):
            """The file descriptor associated with the inotify instance."""
            return self._inotify_fd

        def add_watch(self, path):
            """
            Adds a watch for the given path.

            :param path:
                Path to begin monitoring.
            """
            with self._lock:
                path = absolute_path(path)
                self._add_watch(path, self._event_mask)

        def remove_watch(self, path):
            """
            Removes a watch for the given path.

            :param path:
                Path string for which the watch will be removed.
            """
            with self._lock:
                path = absolute_path(path)
                self._remove_watch(path)

        def close(self):
            """
            Closes the inotify instance and removes all associated watches.
            """
            with self._lock:
                self._remove_all_watches()
                os.close(self._inotify_fd)

        def read_events(self, event_buffer_size=DEFAULT_EVENT_BUFFER_SIZE):
            """
            Reads events from inotify and yields them.
            """
            event_buffer = os.read(self._inotify_fd, event_buffer_size)
            for wd, mask, cookie, name in Inotify._parse_event_buffer(event_buffer):
                yield InotifyEvent(wd, mask, cookie, name)

        # Non-synchronized methods.
        def _add_dir_watch(self, path, recursive, mask):
            """
            Adds a watch (optionally recursively) for the given directory path
            to monitor events specified by the mask.

            :param path:
                Path to monitor
            :param recursive:
                ``True`` to monitor recursively.
            :param mask:
                Event bit mask.
            """
            if not os.path.isdir(path):
                raise OSError('Path is not a directory')
            self._add_watch(path, mask)
            if recursive:
                for root, dirnames, filenames in os.walk(path):
                    for dirname in dirnames:
                        full_path = absolute_path(os.path.join(root, dirname))
                        self._add_watch(full_path, mask)

        def _add_watch(self, path, mask):
            """
            Adds a watch for the given path to monitor events specified by the
            mask.

            :param path:
                Path to monitor
            :param mask:
                Event bit mask.
            """
            wd = inotify_add_watch(self._inotify_fd,
                                            path,
                                            mask)
            if wd == -1:
                Inotify._raise_error()
            self._wd_for_path[path] = wd
            #return wd

        def _remove_all_watches(self):
            """
            Removes all watches.
            """
            for wd in self._wd_for_path.values():
                if inotify_rm_watch(self._inotify_fd, wd) == -1:
                    Inotify._raise_error()

        def _remove_watch(self, path):
            """
            Removes a watch for the given path.

            :param path:
                Path to remove the watch for.
            """
            wd = self._wd_for_path.pop(path)
            if inotify_rm_watch(self._inotify_fd, wd) == -1:
                Inotify._raise_error()

        @staticmethod
        def _raise_error():
            """
            Raises errors for inotify failures.
            """
            _errnum = get_errno()
            raise OSError(strerror(_errnum))

        @staticmethod
        def _parse_event_buffer(buffer):
            """
            Parses an event buffer of ``inotify_event`` structs returned by
            inotify::

                struct inotify_event {
                    __s32 wd;            /* watch descriptor */
                    __u32 mask;          /* watch mask */
                    __u32 cookie;        /* cookie to synchronize two events */
                    __u32 len;           /* length (including nulls) of name */
                    char  name[0];       /* stub for possible name */
                };

            The ``cookie`` member of this struct is used to pair two related events,
            for example, it pairs an IN_MOVED_FROM event with an IN_MOVED_TO event.
            """
            i = 0
            while i + 16 < len(buffer):
                wd, mask, cookie, length = struct.unpack_from('iIII', buffer, i)
                name = buffer[i + 16:i + 16 + length].rstrip('\0')
                i += 16 + length
                yield wd, mask, cookie, name



    class InotifyEmitter(EventEmitter):
        """
        inotify(7)-based event emitter.

        :param event_queue:
            The event queue to fill with events.
        :param watch:
            A watch object representing the directory to monitor.
        :type watch:
            :class:`watchdog.observers.api.ObservedWatch`
        :param timeout:
            Read events blocking timeout (in seconds).
        :type timeout:
            ``float``
        """
        def __init__(self, event_queue, watch, timeout=DEFAULT_EMITTER_TIMEOUT):
            EventEmitter.__init__(self, event_queue, watch, timeout)
            self._lock = threading.Lock()
            self._inotify = Inotify()

        def on_thread_exit(self):
            self._inotify.close()

        def queue_events(self, timeout):
            inotify_events = self._inotify.read_events(event_buffer_size)


    class InotifyObserver(BaseObserver):
        """
        Observer thread that schedules watching directories and dispatches
        calls to event handlers.
        """
        def __init__(self, timeout=DEFAULT_OBSERVER_TIMEOUT):
            BaseObserver.__init__(self, emitter_class=InotifyEmitter, timeout=timeout)
