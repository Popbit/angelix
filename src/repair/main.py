import os
from os.path import join, exists, abspath
import shutil
import argparse
import time
import json
import logging

from project import Validation, Frontend, Backend, Golden, CompilationError
from utils import format_time, time_limit, TimeoutException, Dump, Trace
from transformation import RepairableTransformer, SuspiciousTransformer, FixInjector
from testing import Tester
from localization import Localizer
from reduction import Reducer
from inference import Inferrer, InferenceError
from synthesis import Synthesizer


logger = logging.getLogger(__name__)


class Angelix:

    def __init__(self, working_dir, src, buggy, oracle, tests, golden, output, lines, build, config):
        self.config = config
        self.test_suite = tests.keys()
        extracted = join(working_dir, 'extracted')
        os.mkdir(extracted)

        self.angelic_forest_file = join(working_dir, 'last-angelic-forest.json')

        self.run_test = Tester(config, oracle)
        self.groups_of_suspicious = Localizer(config, lines)
        self.reduce = Reducer(config)
        self.infer_spec = Inferrer(config, tests)
        self.synthesize_fix = Synthesizer(config, extracted)
        self.instrument_for_localization = RepairableTransformer(config)
        self.instrument_for_inference = SuspiciousTransformer(config, extracted)
        self.apply_patch = FixInjector(config)

        validation_dir = join(working_dir, "validation")
        shutil.copytree(src, validation_dir)
        self.validation_src = Validation(config, validation_dir, buggy, build, tests)
        compilation_db = self.validation_src.export_compilation_db()
        self.validation_src.import_compilation_db(compilation_db)

        frontend_dir = join(working_dir, "frontend")
        shutil.copytree(src, frontend_dir)
        self.frontend_src = Frontend(config, frontend_dir, buggy, build, tests)
        self.frontend_src.import_compilation_db(compilation_db)

        backend_dir = join(working_dir, "backend")
        shutil.copytree(src, backend_dir)
        self.backend_src = Backend(config, backend_dir, buggy, build, tests)
        self.backend_src.import_compilation_db(compilation_db)

        if golden is not None:
            golden_dir = join(working_dir, "golden")
            shutil.copytree(golden, golden_dir)
            self.golden_src = Golden(config, golden_dir, buggy, build, tests)
            self.golden_src.import_compilation_db(compilation_db)
        else:
            self.golden_src = None

        self.dump = Dump(working_dir, output)
        self.trace = Trace(working_dir)

    def generate_patch(self):

        def evaluate(src):
            positive = []
            negative = []
            for test in self.test_suite:
                src.build_test(test)
                if self.run_test(src, test):
                    positive.append(test)
                else:
                    negative.append(test)
            return positive, negative

        self.validation_src.build()
        positive, negative = evaluate(self.validation_src)

        self.instrument_for_localization(self.frontend_src)
        self.frontend_src.build()
        logger.info('running positive tests for debugging')
        for test in positive:
            self.frontend_src.build_test(test)
            self.trace += test
            if test not in self.dump:
                self.dump += test
                self.run_test(self.frontend_src, test, dump=self.dump[test], trace=self.trace[test])
            else:
                self.run_test(self.frontend_src, test, trace=self.trace[test])

        if self.golden_src is not None:
            self.golden_src.build()

        logger.info('running negative tests for debugging')
        for test in negative:
            self.frontend_src.build_test(test)
            self.trace += test
            self.run_test(self.frontend_src, test, trace=self.trace[test])
            if test not in self.dump:
                if self.golden_src is None:
                    logger.error("golden version or correct output needed for test {}".format(test))
                    return None
                self.golden_src.build_test(test)
                self.dump += test
                logger.info('running golden version with test {}'.format(test))
                self.run_test(self.golden_src, test, dump=self.dump[test])

        positive_traces = [(test, self.trace.parse(test)) for test in positive]
        negative_traces = [(test, self.trace.parse(test)) for test in negative]
        suspicious = self.groups_of_suspicious(positive_traces, negative_traces)

        while len(negative) > 0 and len(suspicious) > 0:
            expressions = suspicious.pop()
            for e in expressions:
                logger.info('considering suspicious expression {}'.format(e))
            repair_suite = self.reduce(positive_traces, negative_traces, expressions)
            self.backend_src.restore_buggy()
            self.instrument_for_inference(self.backend_src, expressions)
            self.backend_src.build()
            for test in repair_suite:
                self.backend_src.build_test(test)
            angelic_forest = dict()
            inference_failed = False
            for test in repair_suite:
                angelic_forest[test] = self.infer_spec(self.backend_src, test, self.dump[test])
                if len(angelic_forest[test]) == 0:
                    inference_failed = True
                    break
            if inference_failed:
                continue
            with open(self.angelic_forest_file, 'w') as file:
                json.dump(angelic_forest, file, indent=2)    
            initial_fix = self.synthesize_fix(angelic_forest)
            if initial_fix is None:
                logger.info('cannot synthesize fix')
                continue
            logger.info('candidate fix synthesized')
            self.validation_src.restore_buggy()
            self.apply_patch(self.validation_src, initial_fix)
            self.validation_src.build()
            pos, neg = evaluate(self.validation_src)
            if set(neg).isdisjoint(set(repair_suite)):
                not_repaired = list(set(repair_suite) & set(neg))
                logger.warning("generated invalid fix (tests {} not repaired)".format(not_repaired))
            positive, negative = pos, neg

            while len(negative) > 0:
                counterexample = negative.pop()
                logger.info('counterexample test is {}'.format(counterexample))
                repair_suite.append(counterexample)
                self.backend_src.build_test(counterexample)
                angelic_forest[counterexample] = self.infer_spec(self.backend_src,
                                                                 counterexample,
                                                                 self.dump[counterexample])
                if len(angelic_forest[counterexample]) == 0:
                    break
                with open(self.angelic_forest_file, 'w') as file:
                    json.dump(angelic_forest, file, indent=2)    
                fix = self.synthesize_fix(angelic_forest)
                if fix is None:
                    logger.info('cannot refine fix')
                    break
                logger.info('refined fix is synthesized')
                self.validation_src.restore_buggy()
                self.apply_patch(self.validation_src, fix)
                self.validation_src.build()
                pos, neg = evaluate(self.validation_src)
                if set(neg).isdisjoint(set(repair_suite)):
                    not_repaired = list(set(repair_suite) & set(neg))
                    logger.warning("generated invalid fix (tests {} not repaired)".format(not_repaired))
                positive, negative = pos, neg

        if len(negative) > 0:
            return None
        else:
            return self.validation_src.diff_buggy()


if __name__ == "__main__":

    parser = argparse.ArgumentParser('angelix')
    parser.add_argument('src', help='source directory')
    parser.add_argument('buggy', help='relative path to buggy file')
    parser.add_argument('oracle', help='oracle script')
    parser.add_argument('tests', help='tests JSON database')
    parser.add_argument('--golden', metavar='DIR', help='golden source directory')
    parser.add_argument('--output', metavar='FILE', help='correct output for failing test cases')
    parser.add_argument('--defect', metavar='CLASS', nargs='*',
                        default=['condition', 'assignment'],
                        help='defect classes (default: condition assignment)')
    parser.add_argument('--lines', metavar='LINE', nargs='*', help='suspicious lines (default: all)')
    parser.add_argument('--build', metavar='CMD', default='make -e',
                        help='build command in the form of simple shell command (default: %(default)s)')
    parser.add_argument('--timeout', metavar='MS', type=int, default=100000,
                        help='total repair timeout (default: %(default)s)')
    parser.add_argument('--initial-tests', metavar='NUM', type=int, default=3,
                        help='initial repair test suite size (default: %(default)s)')
    parser.add_argument('--test-timeout', metavar='MS', type=int, default=10000,
                        help='test case timeout (default: %(default)s)')
    parser.add_argument('--suspicious', metavar='NUM', type=int, default=5,
                        help='number of suspicious repaired at ones (default: %(default)s)')
    parser.add_argument('--iterations', metavar='NUM', type=int, default=4,
                        help='number of iterations through suspicious (default: %(default)s)')
    parser.add_argument('--localization', default='jaccard', choices=['jaccard', 'ochiai', 'tarantula'],
                        help='formula for localization algorithm (default: %(default)s)')
    parser.add_argument('--klee-forks', metavar='NUM', type=int, default=1000,
                        help='KLEE max number of forks (default: %(default)s)')
    parser.add_argument('--klee-timeout', metavar='MS', type=int, default=0,
                        help='KLEE timeout (default: %(default)s)')
    parser.add_argument('--klee-solver-timeout', metavar='MS', type=int, default=0,
                        help='KLEE solver timeout (default: %(default)s)')
    parser.add_argument('--synthesis-timeout', metavar='MS', type=int, default=10000,
                        help='synthesis timeout (default: %(default)s)')
    parser.add_argument('--synthesis-levels', metavar='LEVEL', nargs='*',
                        default=['alternative', 'integer', 'boolean', 'comparison'],
                        help='component levels (default: alternative integer boolean comparison)')
    parser.add_argument('--verbose', action='store_true',
                        help='print compilation and KLEE messages (default: %(default)s)')
    parser.add_argument('--quiet', action='store_true',
                        help='print only errors (default: %(default)s)')

    args = parser.parse_args()

    if args.quiet:
        logging.basicConfig(level=logging.WARNING)
    else:
        logging.basicConfig(level=logging.INFO)

    working_dir = join(os.getcwd(), ".angelix")
    if exists(working_dir):
        shutil.rmtree(working_dir)
    os.mkdir(working_dir)

    with open(args.tests) as tests_file:
        tests = json.load(tests_file)

    if args.output is not None:
        with open(args.output) as output_file:
            output = json.load(output_file)
    else:
        output = None

    config = dict()
    config['initial_tests']       = args.initial_tests
    config['defect']              = args.defect
    config['test_timeout']        = args.test_timeout
    config['suspicious']          = args.suspicious
    config['iterations']          = args.iterations
    config['localization']        = args.localization
    config['klee_forks']          = args.klee_forks
    config['klee_timeout']        = args.klee_timeout
    config['klee_solver_timeout'] = args.klee_solver_timeout
    config['synthesis_timeout']   = args.synthesis_timeout
    config['synthesis_levels']    = args.synthesis_levels
    config['verbose']             = args.verbose

    tool = Angelix(working_dir,
                   src=args.src,
                   buggy=args.buggy,
                   oracle=abspath(args.oracle),
                   tests=tests,
                   golden=args.golden,
                   output=output,
                   lines=args.lines,
                   build=args.build,
                   config=config)

    start = time.time()

    try:
        with time_limit(args.timeout):
            patch = tool.generate_patch()
    except TimeoutException:
        logger.info("failed to generate patch (timeout)")
        print('TIMEOUT')
        exit(0)
    except (CompilationError, InferenceError):
        logger.info("failed to generate patch")
        print('FAIL')
        exit(1)

    end = time.time()
    elapsed = format_time(end - start)

    if patch is None:
        logger.info("no patch generated in {}".format(elapsed))
        print('FAIL')
        exit(0)
    else:
        logger.info("patch successfully generated in {} (see generated.diff)".format(elapsed))
        print('SUCCESS')
        with open('generated.diff', 'w+') as file:
            for line in patch:
                file.write(line)
        exit(0)
