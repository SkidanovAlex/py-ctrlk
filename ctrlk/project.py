from clang.cindex import Index, Config, TranslationUnitLoadError, CursorKind, File, SourceLocation, Cursor, TranslationUnit
from ctrlk import indexer
import multiprocessing
import threading
import os
import time
import re
import sys

from ctrlk import search

try:
    import simplejson as json
except ImportError:
    import json

def GetCursorForFile(tu, fileName):
    cursor = tu.cursor
    if str(cursor.extent.start.file) == str(cursor.extent.end.file) and os.path.abspath(str(cursor.extent.start.file)) == fileName:
        return cursor

    # TODO
    return None

def RemoveNonAscii(text):
    return re.sub(r'[^\x00-\x7F]+',' ', text)

def SafeSpelling(ch):
    try:
        return ch.spelling
    except ValueError:
        return None

def PopulateScopeNames(cursor, scopeNames, scopeDepths, depth = 0):
    if cursor is None:
        return
    for ch in cursor.get_children():
        if ch.extent and ch.extent.start and ch.extent.end and cursor.extent and cursor.extent.end:
            if str(ch.extent.end.file) == str(cursor.extent.end.file):
                if SafeSpelling(ch) is not None:
                    for i in range(ch.extent.start.line, ch.extent.end.line + 1):
                        while len(scopeNames) <= i:
                            scopeNames.append('')
                            scopeDepths.append(-1)

                        if scopeDepths[i] < depth: 
                            scopeDepths[i] = depth
                            if scopeNames[i] != '': scopeNames[i] += '::'
                            scopeNames[i] += ch.spelling

                PopulateScopeNames(ch, scopeNames, scopeDepths, depth + 1)

def ParseCurrentFileThread(project):
    while True:
        with project.c_parse_lock:
            if len(project.c_parse_queue) == 0:
                project.c_parse_cond.wait()
                continue
            work = project.c_parse_queue[0]
            project.c_parse_queue.pop(0)
        project.parse_current_file_internal(work[0], work[1], work[2])

class Project(object):
    def __init__(self, library_path, project_root, n_workers=None):
        if n_workers is None:
            n_workers = (multiprocessing.cpu_count() * 3) / 2

        self.clang_library_path = library_path

        if not Config.loaded:
            Config.set_library_path(self.clang_library_path)
            Config.set_compatibility_check(False)

        self.builtin_header_path = getBuiltinHeaderPath(self.clang_library_path)

        if self.builtin_header_path is None:
            raise Exception("Cannot find clang includes")

        project_root = os.path.abspath(project_root)

        curr_path = project_root
        self.compile_commands_path = None
        while curr_path:
            compile_commands_path = os.path.join(curr_path, 'compile_commands.json')
            if os.path.exists(compile_commands_path):
                self.compile_commands_path = compile_commands_path
                self.index_db_path = os.path.join(curr_path, '.ctrlk-index')
                self.project_root = curr_path
                break
            elif curr_path == '/':
                break
            curr_path = os.path.dirname(curr_path)

        if self.compile_commands_path is None:
            raise Exception("Could not find a 'compile_commands.json' file in the " +\
                                "directory hierarchy from '%s'" % (project_root))

        self._compilation_db = None
        self._compilation_db_modtime = 0

        self._leveldb_connection = None
        indexer.start(self.leveldb_connection, n_workers)

        self.current_file_tus = {}
        self.current_file_expire = {}
        self.current_file_scopes = {}

        self.c_parse_queue = []
        self.c_parse_lock = threading.Lock()
        self.c_parse_cond = threading.Condition(self.c_parse_lock)

        threading.Thread(target=ParseCurrentFileThread, args=(self,)).start()

    @property
    def leveldb_connection(self):
        if not self._leveldb_connection:
            self._leveldb_connection = indexer.LevelDB(self.index_db_path)
        return self._leveldb_connection

    @property
    def compilation_db(self):
        if self._compilation_db is None \
                or (os.path.exists(self.compile_commands_path) and get_file_modtime(self.compile_commands_path) > self._compilation_db_modtime):
            try:
                with open(self.compile_commands_path, 'r') as f:
                    raw = json.load(f)
            except IOError as e:
                print >>sys.stderr, "Unable to open compile commands path %s: %s" % (self.compile_commands_path, e)

            self._compilation_db = {}
            for entry in raw:
                if 'command' in entry and 'file' in entry:
                    command = entry['command'].split()
                    if '++' in command[0] or "cc" in command[0] or "clang" in command[0]:
                        file_name = os.path.abspath(entry['file'])

                        # it could be startswith in the general case, but for my 
                        # specific purposes I needed to check the middle of the string too -- AS
                        if "/usr/include" in file_name:
                            continue

                        if not os.path.exists(file_name):
                            continue

                        self._compilation_db[file_name] = command + ["-I" + self.builtin_header_path]
        return self._compilation_db

    def get_file_args(self, file_name):
        mod_time = get_file_modtime(file_name)
        compile_command = None
        if file_name in self.compilation_db:
            origin_file = file_name
            compile_command = self.compilation_db[file_name]
        else:
            try:
                origin_file = self.leveldb_connection.Get("h%%%" + file_name)
            except KeyError:        
                return None, None, None
            compile_command = self.compilation_db[origin_file]

        return origin_file, compile_command, mod_time

    def cleanup_expired_tus(self):
        now = time.time()
        with self.c_parse_lock:
            for file_name, expires in self.current_file_expire.iteritems():
                if expires < now:
                    self.current_file_tus.pop(file_name, None)
                    self.current_file_expire.pop(file_name, None)
                    self.current_file_scopes.pop(file_name, None)

    def parse_file(self, file_name):
        try:
            origin_file, compile_command, mod_time = self.get_file_args(file_name)
        except OSError as e:
            if e.errno != 2:
                raise
            else:
                print >>sys.stderr, "Unable to stat() %s: %s" % (file_name, e)
                return

        indexer.add_file_to_parse(origin_file, compile_command, mod_time)

    def parse_current_file(self, command, file_name, content):
        with self.c_parse_lock:
            self.c_parse_queue.append([command, file_name, content])
            self.c_parse_cond.notify()

    # this is called from a different thread
    def parse_current_file_internal(self, command, file_name, content):
        self.cleanup_expired_tus()

        index = Index.create()
        tu = index.parse(None, json.loads(command), unsaved_files=[(file_name, RemoveNonAscii(content))], options = TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD)
        with self.c_parse_lock:
            self.current_file_tus[file_name] = tu
            self.current_file_expire[file_name] = time.time() + 3600 * 10

        scopeNames = []
        scopeDepths = []
        PopulateScopeNames(GetCursorForFile(tu, os.path.abspath(file_name)), scopeNames, scopeDepths)

        with self.c_parse_lock:
            self.current_file_scopes[file_name] = scopeNames

    def unload_current_file(self, file_name):
        with self.c_parse_lock:
            self.current_file_tus.pop(file_name, None)
            self.current_file_expire.pop(file_name, None)
            self.current_file_scopes.pop(file_name, None)
    
    def get_usr_under_cursor(self, file_name, line, col):
        with self.c_parse_lock:
            if file_name not in self.current_file_tus:
                return ""
            tu = self.current_file_tus[file_name]
        f = File.from_name(tu, file_name)
        loc = SourceLocation.from_position(tu, f, int(line), int(col))
        cursor = Cursor.from_location(tu, loc)

        while cursor is not None and (not cursor.referenced or not cursor.referenced.get_usr()):
            nextCursor = cursor.lexical_parent
            if nextCursor is not None and nextCursor == cursor:
                return ""
            cursor = nextCursor
        if cursor is None:
            return ""

        cursor = cursor.referenced
        if cursor is None:
            return ""

        return {'usr': cursor.get_usr(), 'file': str(cursor.location.file), 'line': cursor.location.line, 'column': cursor.location.column}

    def get_current_scope_str(self, file_name, line):
        line = int(line)
        with self.c_parse_lock:
            if file_name in self.current_file_scopes and line < len(self.current_file_scopes[file_name]):
                return self.current_file_scopes[file_name][line]
        return "(no scope)"

    def scan_and_index(self):
        project_files = self.compilation_db
        for file_name, compile_command in project_files.items():
            try:
                mod_time = get_file_modtime(file_name)
            except OSError:
                continue
            indexer.add_file_to_parse(file_name, compile_command, mod_time)

        cpp_files_to_reparse = set()
        for header_file_key, origin_file_name in search.leveldb_range_iter(self.leveldb_connection, "h%%%"):
            header_file_name = search.extract_part(header_file_key, 1)
            saved_mod_time = int(self.leveldb_connection.Get("f%%%" + header_file_name))

            try:
                real_mod_time = get_file_modtime(header_file_name)
            except OSError:
                indexer.remove_file_symbols(header_file_name)
                continue

            if real_mod_time <= saved_mod_time:
                continue

            compile_command = project_files[origin_file_name]
            if origin_file_name not in cpp_files_to_reparse:
                cpp_files_to_reparse.add(origin_file_name)
                indexer.add_file_to_parse(origin_file_name, compile_command, real_mod_time)

    def wait_on_work(self):
        indexer.wait_on_work()

    def work_queue_size(self):
        return indexer.work_queue_size()

def get_file_modtime(file_name):
    return int(os.path.getmtime(file_name))

# the following two functions are taken from clang_complete plugin
def canFindBuiltinHeaders(index, args = []):
  flags = 0
  currentFile = ("test.c", '#include "stddef.h"')
  try:
    tu = index.parse("test.c", args, [currentFile], flags)
  except TranslationUnitLoadError:
    return 0
  return len(tu.diagnostics) == 0

# Derive path to clang builtin headers.
#
# This function tries to derive a path to clang's builtin header files. We are
# just guessing, but the guess is very educated. In fact, we should be right
# for all manual installations (the ones where the builtin header path problem
# is very common) as well as a set of very common distributions.
def getBuiltinHeaderPath(library_path):
  index = Index.create()
  knownPaths = [
          library_path + "/../lib/clang", # default value
          library_path + "/../clang", # gentoo
          library_path + "/clang", # opensuse
          library_path + "/", # Google
          "/usr/lib64/clang", # x86_64 (openSUSE, Fedora)
          "/usr/lib/clang"
  ]

  for path in knownPaths:
    try:
      files = os.listdir(path)
      if len(files) >= 1:
        files = sorted(files)
        subDir = files[-1]
      else:
        subDir = '.'
      path = path + "/" + subDir + "/include/"
      arg = "-I" + path
      if canFindBuiltinHeaders(index, [arg]):
        return path
    except Exception:
      pass

  return None
