import argparse
import signal
import sys

import psycopg

from . import db, pipeline
from .source import Source


def nonnegative(value):
    value = int(value)
    if value < 0:
        raise argparse.ArgumentTypeError('must be nonnegative')
    return value


def positive(value):
    value = int(value)
    if value <= 0:
        raise argparse.ArgumentTypeError('must be positive')
    return value


def main():
    parser = argparse.ArgumentParser(description='Load Georgia health inspections')
    parser.add_argument('--start-page', type=nonnegative)
    parser.add_argument('--max-pages', type=nonnegative)
    parser.add_argument('--full', action='store_true')
    parser.add_argument('--overlap-days', type=nonnegative, default=14)
    parser.add_argument('--reports', action='store_true')
    parser.add_argument('--retry-report-errors', action='store_true')
    parser.add_argument('--max-reports', type=positive)
    args = parser.parse_args()
    if args.retry_report_errors and (args.reports or args.full or args.start_page is not None or args.max_pages is not None):
        parser.error('--retry-report-errors cannot be combined with --reports, --full, --start-page or --max-pages')
    if args.start_page is None:
        args.start_page = 0
    if args.max_reports is not None and not (args.reports or args.retry_report_errors):
        parser.error('--max-reports requires --reports or --retry-report-errors')
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    with psycopg.connect(autocommit=True) as conn:
        if not conn.execute('SELECT pg_try_advisory_lock(%s)', (db.RUN_LOCK_KEY,)).fetchone()[0]:
            print('Another ETL run holds the advisory lock', file=sys.stderr)
            return 1
        db.init(conn)
        conn.execute("UPDATE etl_runs SET status='interrupted', finished=now(), error='Process ended without finishing' WHERE status='running'")
        run_id = db.start(conn, vars(args))
        try:
            with Source() as source:
                status = pipeline.run(conn, source, run_id, vars(args))
            db.finish(conn, run_id, status)
            return 0
        except KeyboardInterrupt:
            db.finish(conn, run_id, 'interrupted', 'Interrupted')
            return 130
        except Exception as exc:
            db.finish(conn, run_id, 'failed', str(exc))
            print(f'ETL failed: {exc}', file=sys.stderr)
            return 1
        finally:
            try:
                status, next_page, counters, conversions, report_errors, options = conn.execute(
                    'SELECT status, next_page, counters, conversion_errors, report_errors, options FROM etl_runs WHERE id=%s',
                    (run_id,)).fetchone()
                print(f'status={status} next_page={next_page} cutoff={options.get("cutoff")} counters={counters} conversion_errors={conversions} report_errors={report_errors}', flush=True)
            except Exception as exc:
                print(f'Could not query run summary: {exc}', file=sys.stderr)


if __name__ == '__main__':
    sys.exit(main())
