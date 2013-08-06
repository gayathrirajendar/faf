# Copyright (C) 2013  ABRT Team
# Copyright (C) 2013  Red Hat, Inc.
#
# This file is part of faf.
#
# faf is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# faf is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with faf.  If not, see <http://www.gnu.org/licenses/>.

import satyr
from hashlib import sha1
from pyfaf.problemtypes import ProblemType
from pyfaf.checker import (Checker,
                           DictChecker,
                           IntChecker,
                           ListChecker,
                           StringChecker)
from pyfaf.common import FafError, log
from pyfaf.queries import (get_backtrace_by_hash,
                           get_kernelmodule_by_name,
                           get_symbol_by_name_path,
                           get_symbolsource,
                           get_taint_flag_by_ureport_name)
from pyfaf.storage import (KernelModule,
                           KernelTaintFlag,
                           Report,
                           ReportBacktrace,
                           ReportBtFrame,
                           ReportBtHash,
                           ReportBtKernelModule,
                           ReportBtTaintFlag,
                           ReportBtThread,
                           OpSysComponent,
                           Symbol,
                           SymbolSource,
                           column_len)
from pyfaf.utils.parse import str2bool

__all__ = ["KerneloopsProblem"]


class KerneloopsProblem(ProblemType):
    name = "kerneloops"
    nice_name = "Kernel oops"

    tainted_flags = {
        "module_proprietary": ("P", "Proprietary module has been loaded"),
        "forced_module": ("F", "Module has been forcibly loaded"),
        "smp_unsafe": ("S", "SMP with CPUs not designed for SMP"),
        "forced_removal": ("R", "User forced a module unload"),
        "mce": ("M", "System experienced a machine check exception"),
        "page_release": ("B", "System has hit bad_page"),
        "userspace": ("U", "Userspace-defined naughtiness"),
        "died_recently": ("D", "Kernel has oopsed before"),
        "acpi_overridden": ("A", "ACPI table overridden"),
        "warning": ("W", "Taint on warning"),
        "staging_driver": ("C", "Modules from drivers/staging are loaded"),
        "firmware_workaround": ("I", "Working around severe firmware bug"),
        "module_out_of_tree": ("O", "Out-of-tree module has been loaded"),
    }

    modname_checker = StringChecker(pattern=r"^[a-zA-Z0-9_]+(\([A-Z\+]+\))?$")

    checker = DictChecker({
        # no need to check type twice, the toplevel checker already did it
        # "type": StringChecker(allowed=[KerneloopsProblem.name]),
        "component":   StringChecker(pattern=r"^kernel(-[a-zA-Z0-9\-\._]+)?$",
                                     maxlen=column_len(OpSysComponent,
                                                       "name")),

        "raw_oops":    StringChecker(maxlen=Report.__lobs__["oops"]),

        "taint_flags": ListChecker(StringChecker(allowed=tainted_flags.keys())),

        "modules":     ListChecker(modname_checker),

        "frames":      ListChecker(DictChecker({
            "address":         IntChecker(minval=0),
            "reliable":        Checker(bool),
            "function_name":   StringChecker(pattern=r"^[a-zA-Z0-9_\.]+$",
                                             maxlen=column_len(Symbol,
                                                               "name")),
            "function_offset": IntChecker(minval=0),
            "function_length": IntChecker(minval=0),
        }), minlen=1)
    })

    @classmethod
    def install(cls, db, logger=None):
        if logger is None:
            logger = log

        for flag, (char, nice_name) in cls.tainted_flags.items():
            if get_taint_flag_by_ureport_name(db, flag) is None:
                logger.info("Adding kernel taint flag '{0}': {1}"
                            .format(char, nice_name))

                new = KernelTaintFlag()
                new.character = char
                new.ureport_name = flag
                new.nice_name = nice_name
                db.session.add(new)

        db.session.flush()

    @classmethod
    def installed(cls, db):
        for flag in cls.tainted_flags.keys():
            if get_taint_flag_by_ureport_name(db, flag) is None:
                return False

        return True

    def __init__(self, *args, **kwargs):
        super(KerneloopsProblem, self).__init__()

        hashkeys = ["processing.oopshashframes", "processing.hashframes"]
        self.load_config_to_self("hashframes", hashkeys, 16, callback=int)

        cmpkeys = ["processing.oopscmpframes", "processing.cmpframes",
                   "processing.clusterframes"]
        self.load_config_to_self("cmpframes", cmpkeys, 16, callback=int)

        cutkeys = ["processing.oopscutthreshold", "processing.cutthreshold"]
        self.load_config_to_self("cutthreshold", cutkeys, 0.3, callback=float)

        normkeys = ["processing.oopsnormalize", "processing.normalize"]
        self.load_config_to_self("normalize", normkeys, True, callback=str2bool)

        self.add_lob = {}

    def _hash_koops(self, koops, taintflags=None, skip_unreliable=False):
        if taintflags is None:
            taintflags = []

        if skip_unreliable:
            frames = filter(lambda f: f["reliable"], koops)
        else:
            frames = koops

        if len(frames) < 1:
            return None

        hashbase = list(taintflags)
        for frame in frames:
            if not "module_name" in frame:
                module = "vmlinux"
            else:
                module = frame["module_name"]

            hashbase.append("{0} {1}+{2}/{3} @ {4}"
                            .format(frame["address"], frame["function_name"],
                                    frame["function_offset"],
                                    frame["function_length"], module))

        return sha1("\n".join(hashbase)).hexdigest()

    def _db_report_to_satyr(self, db_report):
        stacktrace = satyr.Kerneloops()

        if len(db_report.backtraces) < 1:
            self.log_warn("Report #{0} has no usable backtraces"
                          .format(db_report.id))
            return None

        db_backtrace = db_report.backtraces[0]

        if len(db_backtrace.threads) < 1:
            self.log_warn("Backtrace #{0} has no usable threads"
                          .format(db_backtrace.id))
            return None

        db_thread = db_backtrace.threads[0]

        if len(db_thread.frames) < 1:
            self.log_warn("Thread #{0} has no usable frames"
                          .format(db_thread.id))
            return None

        for db_frame in db_thread.frames:
            frame = satyr.KerneloopsFrame()
            frame.function_name = db_frame.symbolsource.symbol.name
            frame.address = db_frame.symbolsource.offset
            frame.reliable = db_frame.reliable
            if frame.address < 0:
                frame.address += (1 << 64)

            stacktrace.frames.append(frame)

        if self.normalize:
            stacktrace.normalize()

        return stacktrace

    def validate_ureport(self, ureport):
        KerneloopsProblem.checker.check(ureport)
        for frame in ureport["frames"]:
            if "module_name" in frame:
                KerneloopsProblem.modname_checker.check(frame["module_name"])

        return True

    def hash_ureport(self, ureport):
        hashbase = [ureport["component"]]
        hashbase.extend(ureport["taint_flags"])

        for i, frame in enumerate(ureport["frames"]):
            # Instance of 'KerneloopsProblem' has no 'hashframes' member
            # pylint: disable-msg=E1101
            if i >= self.hashframes:
                break

            if not "module_name" in frame:
                module = "vmlinux"
            else:
                module = frame["module_name"]

            hashbase.append("{0} @ {1}".format(frame["function_name"], module))

        return sha1("\n".join(hashbase)).hexdigest()

    def save_ureport(self, db, db_report, ureport, flush=False):
        bthash1 = self._hash_koops(ureport["frames"], skip_unreliable=False)
        bthash2 = self._hash_koops(ureport["frames"], skip_unreliable=True)

        db_bt1 = get_backtrace_by_hash(db, bthash1)
        if bthash2 is not None:
            db_bt2 = get_backtrace_by_hash(db, bthash2)
        else:
            db_bt2 = None

        if db_bt1 is not None and db_bt2 is not None:
            if db_bt1 != db_bt2:
                raise FafError("Can't reliably get backtrace from bthash")

            db_backtrace = db_bt1
        elif db_bt1 is not None:
            db_backtrace = db_bt1

            if bthash2 is not None:
                db_bthash2 = ReportBtHash()
                db_bthash2.backtrace = db_backtrace
                db_bthash2.hash = bthash2
                db_bthash2.type = "NAMES"
                db.session.add(db_bthash2)
        elif db_bt2 is not None:
            db_backtrace = db_bt2

            db_bthash1 = ReportBtHash()
            db_bthash1.backtrace = db_backtrace
            db_bthash1.hash = bthash1
            db_bthash1.type = "NAMES"
            db.session.add(db_bthash1)
        else:
            db_backtrace = ReportBacktrace()
            db_backtrace.report = db_report
            db.session.add(db_backtrace)

            db_thread = ReportBtThread()
            db_thread.backtrace = db_backtrace
            db_thread.crashthread = True
            db.session.add(db_thread)

            db_bthash1 = ReportBtHash()
            db_bthash1.backtrace = db_backtrace
            db_bthash1.hash = bthash1
            db_bthash1.type = "NAMES"
            db.session.add(db_bthash1)

            if bthash2 is not None and bthash1 != bthash2:
                db_bthash2 = ReportBtHash()
                db_bthash2.backtrace = db_backtrace
                db_bthash2.hash = bthash2
                db_bthash2.type = "NAMES"
                db.session.add(db_bthash2)

            new_symbols = {}
            new_symbolsources = {}

            i = 0
            for frame in ureport["frames"]:
                # OK, this is totally ugly.
                # Frames may contain inlined functions, that would normally
                # require shifting all frames by 1 and inserting a new one.
                # There is no way to do this efficiently with SQL Alchemy
                # (you need to go one by one and flush after each) so
                # creating a space for additional frames is a huge speed
                # optimization.
                i += 10

                if not "module_name" in frame:
                    module = "vmlinux"
                else:
                    module = frame["module_name"]

                db_symbol = get_symbol_by_name_path(db, frame["function_name"],
                                                    module)
                if db_symbol is None:
                    key = (frame["function_name"], module)
                    if key in new_symbols:
                        db_symbol = new_symbols[key]
                    else:
                        db_symbol = Symbol()
                        db_symbol.name = frame["function_name"]
                        db_symbol.normalized_path = module
                        db.session.add(db_symbol)
                        new_symbols[key] = db_symbol

                db_symbolsource = get_symbolsource(db, db_symbol, module,
                                                   frame["address"])
                if db_symbolsource is None:
                    key = (frame["function_name"], module, frame["address"])
                    if key in new_symbolsources:
                        db_symbolsource = new_symbolsources[key]
                    else:
                        db_symbolsource = SymbolSource()
                        db_symbolsource.path = module
                        # this doesn't work well. on 64bit, kernel maps to
                        # the end of address space (64bit unsigned), but in
                        # postgres bigint is 64bit signed and can't save
                        # the value - let's just map it to signed
                        if frame["address"] >= (1 << 63):
                            db_symbolsource.offset = (frame["address"] -
                                                      (1 << 64))
                        else:
                            db_symbolsource.offset = frame["address"]
                        db_symbolsource.symbol = db_symbol
                        db.session.add(db_symbolsource)
                        new_symbolsources[key] = db_symbolsource

                db_frame = ReportBtFrame()
                db_frame.thread = db_thread
                db_frame.order = i
                db_frame.symbolsource = db_symbolsource
                db_frame.inlined = False
                db_frame.reliable = frame["reliable"]
                db.session.add(db_frame)

            for taintflag in ureport["taint_flags"]:
                db_taintflag = get_taint_flag_by_ureport_name(db, taintflag)
                if db_taintflag is None:
                    self.log_warn("Skipping unsupported taint flag '{0}'"
                                  .format(taintflag))
                    continue

                db_bttaintflag = ReportBtTaintFlag()
                db_bttaintflag.backtrace = db_backtrace
                db_bttaintflag.taintflag = db_taintflag
                db.session.add(db_bttaintflag)

            new_modules = {}

            for module in ureport["modules"]:
                idx = module.find("(")
                if idx >= 0:
                    module = module[:idx]

                db_module = get_kernelmodule_by_name(db, module)
                if db_module is None:
                    if module in new_modules:
                        db_module = new_modules[module]
                    else:
                        db_module = KernelModule()
                        db_module.name = module
                        db.session.add(db_module)
                        new_modules[module] = db_module

                db_btmodule = ReportBtKernelModule()
                db_btmodule.kernelmodule = db_module
                db_btmodule.backtrace = db_backtrace
                db.session.add(db_btmodule)

            # do not overwrite an existing oops
            if not db_report.has_lob("oops"):
                # do not append here, but create a new dict
                # we only want save_ureport_post_flush process the most
                # recently saved report
                self.add_lob = {db_report: ureport["raw_oops"]}

        if flush:
            db.session.flush()

    def save_ureport_post_flush(self):
        for report, raw_oops in self.add_lob.items():
            report.save_lob("oops", raw_oops)

        # clear the list so that re-calling does not make problems
        self.add_lob = {}

    def get_component_name(self, ureport):
        return ureport["component"]

    def get_ssources_for_retrace(self, db):
        self.log_warn("Retracing is not yet implemented for kerneloops")
        return []

    def find_packages_for_ssource(self, db, db_ssource):
        self.log_warn("Retracing is not yet implemented for kerneloops")
        return None, (None, None, None)

    def retrace(self, db, task):
        self.log_warn("Retracing is not yet implemented for kerneloops")

    def compare(self, db_report1, db_report2):
        satyr_report1 = self._db_report_to_satyr(db_report1)
        satyr_report2 = self._db_report_to_satyr(db_report2)
        return satyr_report1.distance(satyr_report2)

    def compare_many(self, db_reports):
        self.log_info("Loading reports")

        reports = []
        ret_db_reports = []

        i = 0
        for db_report in db_reports:
            i += 1

            self.log_debug("[{0} / {1}] Loading report #{2}"
                           .format(i, len(db_reports), db_report.id))
            report = self._db_report_to_satyr(db_report)

            if report is None:
                self.log_debug("Unable to build satyr.Kerneloops")
                continue

            reports.append(report)
            ret_db_reports.append(db_report)

        self.log_info("Calculating distances")
        distances = satyr.Distances(reports, len(reports))

        return ret_db_reports, distances

    def check_btpath_match(self, ureport, parser):
        for frame in ureport["frames"]:
            # vmlinux
            if not "module_name" in frame:
                continue

            match = parser.match(frame["module_name"])

            if match is not None:
                return True

        return False