import argparse
import json
import os
import signal
import threading
import time
import tornado.web

from ctrlk import client_api
from ctrlk import project
from ctrlk import search

g_projects = {}
g_last_request_time = time.time()

def get_absolute_path():
    return os.path.abspath(os.path.realpath(__file__))

def killer_thread(suicide_seconds):
    global g_last_request_time
    while True:
        if time.time() - g_last_request_time > suicide_seconds:
            os.kill(os.getpid(), signal.SIGINT)
        time.sleep(10)

class MyRequestHandler(tornado.web.RequestHandler):
    def prepare(self):
        global g_last_request_time
        g_last_request_time = time.time()
    def get_project(self):
        project_root = self.get_argument("project_root")
        abs_project_root = os.path.abspath(project_root)
        return g_projects[abs_project_root]

class PingHandler(MyRequestHandler):
    def get(self):
        self.write("Hello, world!")

class RegisterHandler(MyRequestHandler):
    def get(self):
        global g_projects

        library_path = self.get_argument("library_path")
        project_root = self.get_argument("project_root")

        abs_project_root = os.path.abspath(project_root)

        if abs_project_root not in g_projects:
            g_projects[abs_project_root] = project.Project(library_path, project_root)

class ParseHandler(MyRequestHandler):
    def get(self):
        file_name = self.get_argument("file_name", None)
        if file_name:
            self.get_project().parse_file(file_name)
        else:
            self.get_project().scan_and_index()

class QueueSizeHandler(MyRequestHandler):
    def get(self):
        self.write(json.dumps(self.get_project().work_queue_size()))

class LevelDBSearchHandler(MyRequestHandler):
    def get(self):
        starts_with = self.get_argument('starts_with')
        ret = [x for x in search.leveldb_range_iter(self.get_project().leveldb_connection, starts_with)]
        self.write(json.dumps(ret))

class MatchHandler(MyRequestHandler):
    def get(self):
        prefix = self.get_argument('prefix')
        limit = int(self.get_argument('limit'))
        ret = search.get_items_matching_pattern(self.get_project().leveldb_connection, prefix, limit)
        self.write(json.dumps(ret))

class BuiltinHeaderPathHandler(MyRequestHandler):
    def get(self):
        self.write(json.dumps(self.get_project().builtin_header_path))

class FileArgsHandler(MyRequestHandler):
    def get(self):
        file_name = self.get_argument('file_name')

        origin_file, compile_command, mod_time = self.get_project().get_file_args(file_name)
        self.write(json.dumps(compile_command))

class ParseCurrentFileHandler(MyRequestHandler):
    def post(self):
        command = self.get_argument('command')
        file_name = self.get_argument('file_name')
        content = self.get_argument('content')
        self.get_project().parse_current_file(command, file_name, content)

class UnloadCurrentFileHandler(MyRequestHandler):
    def get(self):
        file_name = self.get_argument('file_name')
        self.get_project().unload_current_file(file_name)

class GetUsrUnderCursorHandler(MyRequestHandler):
    def get(self):
        file_name = self.get_argument('file_name')
        row = self.get_argument('row')
        col = self.get_argument('col')
        ret = self.get_project().get_usr_under_cursor(file_name, row, col)
        self.write(json.dumps(ret))

class GetCurrentScopeStrHandler(MyRequestHandler):
    def get(self):
        file_name = self.get_argument('file_name')
        row = self.get_argument('row')
        ret = self.get_project().get_current_scope_str(file_name, row)
        self.write(json.dumps(ret))

def sigint_handler(signum, frame):
    for v in g_projects.itervalues():
        v.wait_on_work()
    os.kill(os.getpid(), signal.SIGTERM)

application = tornado.web.Application([
    (r"/", PingHandler),
    (r"/register", RegisterHandler),
    (r"/parse", ParseHandler),
    (r"/queue_size", QueueSizeHandler),
    (r"/leveldb_search", LevelDBSearchHandler),
    (r"/match", MatchHandler),
    (r"/builtin_header_path", BuiltinHeaderPathHandler),
    (r"/file_args", FileArgsHandler),
    (r"/parse_current_file", ParseCurrentFileHandler),
    (r"/unload_current_file", UnloadCurrentFileHandler),
    (r"/get_usr_under_cursor", GetUsrUnderCursorHandler),
    (r"/get_current_scope_str", GetCurrentScopeStrHandler),
])

def launch_server(port, suicide_seconds):
    application.listen(port)

    signal.signal(signal.SIGINT, sigint_handler)

    t = threading.Thread(target=killer_thread, args=(suicide_seconds,))
    t.start()

    tornado.ioloop.IOLoop.instance().start()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser = argparse.ArgumentParser(conflict_handler='resolve')
    parser.add_argument('-p', '--port', dest='port', type=int, default=client_api.DEFAULT_PORT)
    parser.add_argument('-s', '--suicide-seconds', dest='suicide_seconds', type=int, default=3600)
    options = parser.parse_args()

    launch_server(options.port, options.suicide_seconds)

