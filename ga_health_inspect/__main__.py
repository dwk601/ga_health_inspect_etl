import argparse
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
    parser.add_argument('--start-page', type=nonnegative, default=0)
    parser.add_argument('--max-pages', type=nonnegative)
    parser.add_argument('--full', action='store_true')
    parser.add_argument('--overlap-days', type=nonnegative, default=14)
    parser.add_argument('--reports', action='store_true')
    parser.add_argument('--max-reports', type=positive)
    args = parser.parse_args()
    if args.max_reports is not None and not args.reports:
        parser.error('--max-reports requires --reports')
    with psycopg.connect(autocommit=True) as conn:
        db.init(conn)
        run_id = db.start(conn, vars(args))
        try:
            status = pipeline.run(conn, Source(), run_id, vars(args))
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
            status, next_page, counters, conversions, report_errors, options = conn.execute(
                'SELECT status, next_page, counters, conversion_errors, report_errors, options FROM etl_runs WHERE id=%s',
                (run_id,)).fetchone()
            print(f'status={status} next_page={next_page} cutoff={options.get("cutoff")} counters={counters} conversion_errors={conversions} report_errors={report_errors}', flush=True)


if __name__ == '__main__':
    sys.exit(main())
