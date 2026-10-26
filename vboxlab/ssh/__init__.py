"""ssh access to guests that have no console on COM1.

PySnap runs the system OpenSSH client (``ssh``) with its own key pair and its
own ``known_hosts`` files, so the user's keys and ``~/.ssh/known_hosts`` are
never read or changed.
"""
