from django.core.management.base import BaseCommand

from leads.models import Lead
from leads.status import compute_lead_status, TERMINAL_STATUSES


class Command(BaseCommand):
    help = "Recompute lead statuses based on current data (demographics, insurance, docs, consent forms)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would change without saving",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        leads = Lead.objects.exclude(status__in=TERMINAL_STATUSES)
        total = leads.count()
        updated = 0
        changes = []

        for lead in leads.iterator():
            new_status = compute_lead_status(lead)
            if new_status and lead.status != new_status:
                changes.append(
                    f"  Lead {lead.uuid} (REF-{lead.id:03d}): "
                    f"{lead.status} -> {new_status}"
                )
                if not dry_run:
                    lead.status = new_status
                    lead.save(update_fields=["status"])
                updated += 1

        prefix = "[DRY RUN] " if dry_run else ""
        if changes:
            self.stdout.write("\n".join(changes))
        self.stdout.write(
            self.style.SUCCESS(
                f"\n{prefix}Processed {total} leads, {updated} updated."
            )
        )
