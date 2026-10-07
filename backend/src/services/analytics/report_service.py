"""
Report Generation Service for analytics reports
"""

import asyncio
import logging
import smtplib
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, BinaryIO, Dict, List, Optional, Union

import aiofiles
import aiohttp
import pandas as pd
from sqlalchemy import and_, delete, desc, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.core.config import settings
from src.core.database import get_async_session
from src.models.analytics.analytics_models import (
    AnalyticsReport,
    MetricQuery,
    MetricQueryResult,
    TimeSeriesData,
)
from src.models.analytics.dashboard_models import Dashboard
from src.models.base import GUID
from src.utils.outbound_url_guard import assert_public_https_url

logger = logging.getLogger(__name__)


class ReportGenerationService:
    """Service for generating analytics reports"""

    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=4)
        self.temp_dir = Path(tempfile.gettempdir()) / "analytics_reports"
        self.temp_dir.mkdir(exist_ok=True)
        self.max_report_size_mb = 50
        self.max_generation_time_minutes = 30

    async def create_report(
        self,
        name: str,
        title: str,
        description: Optional[str],
        owner_id: uuid.UUID,
        organization_id: Optional[uuid.UUID],
        report_config: Dict[str, Any],
        schedule_config: Optional[Dict[str, Any]] = None,
        output_format: str = "pdf",
        delivery_config: Optional[Dict[str, Any]] = None,
    ) -> AnalyticsReport:
        """Create a new analytics report"""
        try:
            async with get_async_session() as db:
                report = AnalyticsReport(
                    name=name,
                    title=title,
                    description=description,
                    owner_id=owner_id,
                    organization_id=organization_id,
                    report_config=report_config,
                    schedule_config=schedule_config,
                    output_format=output_format,
                    delivery_config=delivery_config,
                    is_active=True,
                    is_public=False,
                )
                db.add(report)
                await db.commit()
                await db.refresh(report)

                return report

        except Exception as e:
            logger.error(f"Error creating report: {e}")
            raise

    async def generate_report(self, report_id: uuid.UUID, force: bool = False) -> bool:
        """Generate a report"""
        try:
            async with get_async_session() as db:
                # Get report
                query = select(AnalyticsReport).where(AnalyticsReport.id == report_id)
                result = await db.execute(query)
                report = result.scalar_one_or_none()

                if not report:
                    raise ValueError(f"Report not found: {report_id}")

                if not report.is_active and not force:
                    logger.info(
                        f"Report {report_id} is not active, skipping generation"
                    )
                    return False

                # Check if recently generated (unless forced)
                if (
                    not force
                    and report.last_run_at
                    and (datetime.utcnow() - report.last_run_at) < timedelta(minutes=5)
                ):
                    logger.info(f"Report {report_id} was recently generated, skipping")
                    return False

                # Update status
                await db.execute(
                    update(AnalyticsReport)
                    .where(AnalyticsReport.id == report_id)
                    .values(last_run_status="running", last_run_at=datetime.utcnow())
                )
                await db.commit()

                try:
                    # Generate report content
                    report_data = await self._generate_report_content(
                        report.report_config
                    )

                    # Create output file
                    output_path = await self._create_report_file(report, report_data)

                    # Deliver report if configured
                    if report.delivery_config:
                        await self._deliver_report(report, output_path)

                    # Update status
                    await db.execute(
                        update(AnalyticsReport)
                        .where(AnalyticsReport.id == report_id)
                        .values(
                            last_run_status="completed", last_run_at=datetime.utcnow()
                        )
                    )
                    await db.commit()

                    logger.info(f"Report {report_id} generated successfully")
                    return True

                except Exception as e:
                    logger.error(f"Error generating report {report_id}: {e}")

                    # Update status with error
                    await db.execute(
                        update(AnalyticsReport)
                        .where(AnalyticsReport.id == report_id)
                        .values(
                            last_run_status="failed",
                            last_run_error=str(e),
                            last_run_at=datetime.utcnow(),
                        )
                    )
                    await db.commit()

                    return False

        except Exception as e:
            logger.error(f"Error in generate_report: {e}")
            return False

    async def _generate_report_content(
        self, report_config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Generate report content based on configuration"""
        content = {
            "title": report_config.get("title", "Analytics Report"),
            "subtitle": report_config.get("subtitle", ""),
            "generated_at": datetime.utcnow(),
            "sections": [],
        }

        # Generate sections based on config
        sections_config = report_config.get("sections", [])

        for section_config in sections_config:
            section_type = section_config.get("type")

            if section_type == "summary":
                section = await self._generate_summary_section(section_config)
            elif section_type == "metrics":
                section = await self._generate_metrics_section(section_config)
            elif section_type == "charts":
                section = await self._generate_charts_section(section_config)
            elif section_type == "tables":
                section = await self._generate_tables_section(section_config)
            elif section_type == "dashboard":
                section = await self._generate_dashboard_section(section_config)
            else:
                logger.warning(f"Unknown section type: {section_type}")
                continue

            content["sections"].append(section)

        return content

    async def _generate_summary_section(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Generate summary section"""
        # Get dashboard summary if specified
        dashboard_id = config.get("dashboard_id")
        if dashboard_id:
            async with get_async_session() as db:
                query = select(Dashboard).where(Dashboard.id == dashboard_id)
                result = await db.execute(query)
                dashboard = result.scalar_one_or_none()

                if dashboard:
                    return {
                        "type": "summary",
                        "title": config.get("title", "Dashboard Summary"),
                        "content": {
                            "dashboard_name": dashboard.name,
                            "description": dashboard.description,
                            "widget_count": len(
                                [w for w in dashboard.widgets if w.is_active]
                            ),
                            "last_updated": dashboard.updated_at,
                            "theme": dashboard.theme,
                        },
                    }

        # Default summary
        return {
            "type": "summary",
            "title": config.get("title", "Report Summary"),
            "content": {
                "generated_at": datetime.utcnow(),
                "report_period": config.get("period", "Last 30 days"),
            },
        }

    async def _generate_metrics_section(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Generate metrics section"""
        metric_queries = config.get("metrics", [])
        metrics_data = []

        for metric_config in metric_queries:
            try:
                # Execute metric query
                metric_data = await self._execute_metric_query(metric_config)
                metrics_data.append(metric_data)
            except Exception as e:
                logger.error(f"Error executing metric query: {e}")
                metrics_data.append(
                    {"name": metric_config.get("name", "Unknown"), "error": str(e)}
                )

        return {
            "type": "metrics",
            "title": config.get("title", "Key Metrics"),
            "metrics": metrics_data,
        }

    async def _generate_charts_section(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Generate charts section"""
        charts_config = config.get("charts", [])
        charts_data = []

        for chart_config in charts_config:
            try:
                # Get chart data
                chart_data = await self._get_chart_data(chart_config)
                charts_data.append(chart_data)
            except Exception as e:
                logger.error(f"Error getting chart data: {e}")
                charts_data.append(
                    {
                        "title": chart_config.get("title", "Unknown Chart"),
                        "error": str(e),
                    }
                )

        return {
            "type": "charts",
            "title": config.get("title", "Charts"),
            "charts": charts_data,
        }

    async def _generate_tables_section(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Generate tables section"""
        tables_config = config.get("tables", [])
        tables_data = []

        for table_config in tables_config:
            try:
                # Get table data
                table_data = await self._get_table_data(table_config)
                tables_data.append(table_data)
            except Exception as e:
                logger.error(f"Error getting table data: {e}")
                tables_data.append(
                    {
                        "title": table_config.get("title", "Unknown Table"),
                        "error": str(e),
                    }
                )

        return {
            "type": "tables",
            "title": config.get("title", "Data Tables"),
            "tables": tables_data,
        }

    async def _generate_dashboard_section(
        self, config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Generate dashboard snapshot section"""
        dashboard_id = config.get("dashboard_id")
        if not dashboard_id:
            return {
                "type": "dashboard",
                "title": "Dashboard Snapshot",
                "error": "No dashboard ID specified",
            }

        try:
            async with get_async_session() as db:
                query = (
                    select(Dashboard)
                    .options(selectinload(Dashboard.widgets))
                    .where(Dashboard.id == dashboard_id)
                )
                result = await db.execute(query)
                dashboard = result.scalar_one_or_none()

                if not dashboard:
                    return {
                        "type": "dashboard",
                        "title": "Dashboard Snapshot",
                        "error": f"Dashboard not found: {dashboard_id}",
                    }

                # Get dashboard data
                dashboard_data = {
                    "name": dashboard.name,
                    "description": dashboard.description,
                    "theme": dashboard.theme,
                    "layout": dashboard.layout,
                    "widgets": [],
                }

                # Get widget data (simplified)
                for widget in dashboard.widgets:
                    if widget.is_active:
                        widget_data = await self._get_widget_data(widget)
                        dashboard_data["widgets"].append(widget_data)

                return {
                    "type": "dashboard",
                    "title": config.get("title", f"Dashboard: {dashboard.name}"),
                    "dashboard": dashboard_data,
                }

        except Exception as e:
            logger.error(f"Error generating dashboard section: {e}")
            return {"type": "dashboard", "title": "Dashboard Snapshot", "error": str(e)}

    async def _execute_metric_query(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a metric query"""
        # This is a simplified implementation
        # In a real system, you'd integrate with your metrics service
        metric_name = config.get("name", "Unknown")
        metric_type = config.get("type", "counter")
        time_range = config.get("time_range", "1d")

        # Mock data for demonstration
        return {
            "name": metric_name,
            "type": metric_type,
            "value": 1234.56,
            "change": 5.67,
            "change_percent": 12.3,
            "time_range": time_range,
            "status": "good",  # good, warning, critical
        }

    async def _get_chart_data(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Get chart data"""
        chart_type = config.get("type", "line")
        data_source = config.get("data_source")

        # Mock chart data
        return {
            "title": config.get("title", "Chart"),
            "type": chart_type,
            "data": {
                "labels": ["Jan", "Feb", "Mar", "Apr", "May", "Jun"],
                "datasets": [{"label": "Dataset 1", "data": [12, 19, 3, 5, 2, 3]}],
            },
        }

    async def _get_table_data(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Get table data"""
        # Mock table data
        return {
            "title": config.get("title", "Data Table"),
            "columns": ["Name", "Value", "Change", "Status"],
            "rows": [
                ["Item 1", 100, 5.0, "Good"],
                ["Item 2", 200, -2.0, "Warning"],
                ["Item 3", 150, 0.0, "Good"],
            ],
        }

    async def _get_widget_data(self, widget) -> Dict[str, Any]:
        """Get widget data for report"""
        # Mock widget data - in real implementation, get actual widget data
        return {
            "id": str(widget.id),
            "title": widget.title,
            "type": widget.widget_type,
            "position": {
                "x": widget.x,
                "y": widget.y,
                "width": widget.width,
                "height": widget.height,
            },
            "data": {
                "summary": "Widget data would go here",
                "last_updated": datetime.utcnow(),
            },
        }

    async def _create_report_file(
        self, report: AnalyticsReport, report_data: Dict[str, Any]
    ) -> Path:
        """Create report file in specified format"""
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        filename = f"{report.name}_{timestamp}.{report.output_format}"
        output_path = self.temp_dir / filename

        if report.output_format == "pdf":
            await self._create_pdf_report(output_path, report_data)
        elif report.output_format == "csv":
            await self._create_csv_report(output_path, report_data)
        elif report.output_format == "xlsx":
            await self._create_excel_report(output_path, report_data)
        elif report.output_format == "json":
            await self._create_json_report(output_path, report_data)
        else:
            raise ValueError(f"Unsupported output format: {report.output_format}")

        return output_path

    async def _create_pdf_report(
        self, output_path: Path, report_data: Dict[str, Any]
    ) -> None:
        """Create PDF report"""
        # This is a simplified implementation
        # In production, you'd use a proper PDF library like ReportLab or WeasyPrint

        # For now, create a simple text file as placeholder
        content = self._format_report_as_text(report_data)

        async with aiofiles.open(output_path, "w") as f:
            await f.write(content)

    async def _create_csv_report(
        self, output_path: Path, report_data: Dict[str, Any]
    ) -> None:
        """Create CSV report"""
        # Extract table data and convert to CSV
        rows = []

        # Add header
        rows.append([report_data["title"]])
        rows.append([f"Generated: {report_data['generated_at']}"])
        rows.append([])  # Empty row

        # Process sections
        for section in report_data["sections"]:
            rows.append([section["title"]])
            rows.append([])

            if section["type"] == "metrics":
                rows.append(["Metric", "Value", "Change", "Status"])
                for metric in section["metrics"]:
                    if "error" not in metric:
                        rows.append(
                            [
                                metric["name"],
                                str(metric["value"]),
                                str(metric.get("change", 0)),
                                metric.get("status", ""),
                            ]
                        )
            elif section["type"] == "tables":
                for table in section["tables"]:
                    if "error" not in table:
                        rows.append(table["columns"])
                        for row in table["rows"]:
                            rows.append(row)

            rows.append([])  # Empty row between sections

        # Write CSV
        df = pd.DataFrame(rows)
        df.to_csv(output_path, index=False, header=False)

    async def _create_excel_report(
        self, output_path: Path, report_data: Dict[str, Any]
    ) -> None:
        """Create Excel report"""
        with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
            # Summary sheet
            summary_data = [
                ["Report Title", report_data["title"]],
                ["Generated", report_data["generated_at"]],
                ["Sections", len(report_data["sections"])],
            ]
            pd.DataFrame(summary_data, columns=["Item", "Value"]).to_excel(
                writer, sheet_name="Summary", index=False
            )

            # Section sheets
            for i, section in enumerate(report_data["sections"]):
                sheet_name = f"Section_{i+1}"[:31]  # Excel sheet name limit

                if section["type"] == "metrics":
                    metrics_data = []
                    for metric in section["metrics"]:
                        if "error" not in metric:
                            metrics_data.append(
                                [
                                    metric["name"],
                                    metric["value"],
                                    metric.get("change", 0),
                                    metric.get("status", ""),
                                ]
                            )

                    pd.DataFrame(
                        metrics_data, columns=["Metric", "Value", "Change", "Status"]
                    ).to_excel(writer, sheet_name=sheet_name, index=False)

    async def _create_json_report(
        self, output_path: Path, report_data: Dict[str, Any]
    ) -> None:
        """Create JSON report"""
        import json

        # Convert datetime objects to strings
        def json_serializer(obj):
            if isinstance(obj, datetime):
                return obj.isoformat()
            raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

        async with aiofiles.open(output_path, "w") as f:
            await f.write(json.dumps(report_data, default=json_serializer, indent=2))

    def _format_report_as_text(self, report_data: Dict[str, Any]) -> str:
        """Format report data as plain text"""
        lines = []
        lines.append("=" * 60)
        lines.append(report_data["title"])
        lines.append("=" * 60)
        lines.append(f"Generated: {report_data['generated_at']}")
        lines.append("")

        for section in report_data["sections"]:
            lines.append(f"\n{section['title']}")
            lines.append("-" * len(section["title"]))

            if section["type"] == "summary":
                content = section["content"]
                for key, value in content.items():
                    lines.append(f"{key.replace('_', ' ').title()}: {value}")

            elif section["type"] == "metrics":
                for metric in section["metrics"]:
                    if "error" not in metric:
                        lines.append(f"{metric['name']}: {metric['value']}")
                        if metric.get("change"):
                            lines.append(
                                f"  Change: {metric['change']} ({metric.get('change_percent', 0)}%)"
                            )

            lines.append("")

        return "\n".join(lines)

    async def _deliver_report(self, report: AnalyticsReport, file_path: Path) -> None:
        """Deliver report based on configuration"""
        delivery_config = report.delivery_config
        if not delivery_config:
            return

        delivery_methods = delivery_config.get("methods", [])

        for method in delivery_methods:
            try:
                if method == "email":
                    await self._deliver_email(report, file_path, delivery_config)
                elif method == "webhook":
                    await self._deliver_webhook(report, file_path, delivery_config)
                elif method == "s3":
                    await self._deliver_s3(report, file_path, delivery_config)
                else:
                    logger.warning(f"Unknown delivery method: {method}")
            except Exception as e:
                logger.error(f"Error delivering report via {method}: {e}")

    async def _deliver_email(
        self, report: AnalyticsReport, file_path: Path, config: Dict[str, Any]
    ) -> None:
        """Deliver report via email"""
        email_config = config.get("email", {})
        recipients = email_config.get("recipients", [])

        if not recipients:
            logger.warning("No email recipients specified")
            return

        # Create email
        msg = MIMEMultipart()
        msg["From"] = email_config.get("from", settings.SMTP_USER)
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = f"Analytics Report: {report.title}"

        # Add body
        body = f"""
        Dear User,

        Please find attached the analytics report: {report.title}

        Report Details:
        - Name: {report.name}
        - Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}
        - Format: {report.output_format}

        Best regards,
        Analytics Team
        """

        msg.attach(MIMEText(body, "plain"))

        # Attach file
        async with aiofiles.open(file_path, "rb") as f:
            attachment_data = await f.read()

        attachment = MIMEApplication(attachment_data)
        attachment.add_header(
            "Content-Disposition", "attachment", filename=file_path.name
        )
        msg.attach(attachment)

        # Send email
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            self.executor, self._send_email_sync, msg, recipients
        )

    def _send_email_sync(self, msg: MIMEMultipart, recipients: List[str]) -> None:
        """Send email synchronously"""
        try:
            with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as server:
                if settings.SMTP_USE_TLS:
                    server.starttls()
                if settings.SMTP_USER and settings.SMTP_PASSWORD:
                    server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)

                server.send_message(msg, to_addrs=recipients)
                logger.info(f"Email sent to {recipients}")
        except Exception as e:
            logger.error(f"Error sending email: {e}")
            raise

    async def _deliver_webhook(
        self, report: AnalyticsReport, file_path: Path, config: Dict[str, Any]
    ) -> None:
        """Deliver report via webhook"""
        webhook_config = config.get("webhook", {})
        url = webhook_config.get("url")

        if not url:
            logger.warning("No webhook URL specified")
            return

        # GOO-409: user-supplied destination. Public https only; refuse private,
        # loopback, link-local and metadata addresses (also on resolution).
        # The default resolver blocks, so run the check off the event loop.
        await asyncio.to_thread(assert_public_https_url, url)

        # Prepare payload
        payload = {
            "report_id": str(report.id),
            "report_name": report.name,
            "report_title": report.title,
            "generated_at": datetime.utcnow().isoformat(),
            "format": report.output_format,
        }

        # Send file if configured
        if webhook_config.get("include_file", False):
            async with aiofiles.open(file_path, "rb") as f:
                file_data = await f.read()

            files = {"file": (file_path.name, file_data)}
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, data=payload, files=files, allow_redirects=False
                ) as response:
                    if response.status != 200:
                        logger.error(f"Webhook failed with status {response.status}")
                    else:
                        logger.info("Webhook delivered successfully")
        else:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=payload, allow_redirects=False
                ) as response:
                    if response.status != 200:
                        logger.error(f"Webhook failed with status {response.status}")
                    else:
                        logger.info("Webhook delivered successfully")

    async def _deliver_s3(
        self, report: AnalyticsReport, file_path: Path, config: Dict[str, Any]
    ) -> None:
        """Deliver report to S3"""
        # This is a placeholder for S3 delivery
        # In production, you'd use boto3 or similar
        logger.info(f"S3 delivery not implemented for {file_path}")

    async def list_reports(
        self,
        user_id: uuid.UUID,
        organization_id: Optional[uuid.UUID] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> List[AnalyticsReport]:
        """List reports for user"""
        try:
            async with get_async_session() as db:
                query = (
                    select(AnalyticsReport)
                    .where(
                        and_(
                            AnalyticsReport.is_active == True,
                            or_(
                                AnalyticsReport.owner_id == user_id,
                                AnalyticsReport.is_public == True,
                                and_(
                                    AnalyticsReport.organization_id == organization_id,
                                    organization_id is not None,
                                ),
                            ),
                        )
                    )
                    .order_by(desc(AnalyticsReport.updated_at))
                    .offset(offset)
                    .limit(limit)
                )

                result = await db.execute(query)
                reports = result.scalars().all()
                return list(reports)

        except Exception as e:
            logger.error(f"Error listing reports: {e}")
            return []

    async def get_report(
        self, report_id: uuid.UUID, user_id: uuid.UUID
    ) -> Optional[AnalyticsReport]:
        """Get report by ID"""
        try:
            async with get_async_session() as db:
                query = select(AnalyticsReport).where(AnalyticsReport.id == report_id)
                result = await db.execute(query)
                report = result.scalar_one_or_none()

                if not report:
                    return None

                # Check permissions
                if (
                    report.owner_id != user_id
                    and not report.is_public
                    and report.organization_id
                ):
                    # Would need to check organization membership
                    pass

                return report

        except Exception as e:
            logger.error(f"Error getting report {report_id}: {e}")
            return None

    async def delete_report(self, report_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        """Delete report"""
        try:
            async with get_async_session() as db:
                # Get report
                query = select(AnalyticsReport).where(AnalyticsReport.id == report_id)
                result = await db.execute(query)
                report = result.scalar_one_or_none()

                if not report:
                    return False

                # Check ownership
                if report.owner_id != user_id:
                    raise PermissionError("Only report owner can delete reports")

                # Deactivate report
                await db.execute(
                    update(AnalyticsReport)
                    .where(AnalyticsReport.id == report_id)
                    .values(is_active=False)
                )
                await db.commit()

                return True

        except Exception as e:
            logger.error(f"Error deleting report {report_id}: {e}")
            return False

    async def cleanup_temp_files(self, older_than_hours: int = 24) -> int:
        """Clean up temporary report files"""
        try:
            cutoff_time = datetime.utcnow() - timedelta(hours=older_than_hours)
            cleaned_count = 0

            for file_path in self.temp_dir.iterdir():
                if file_path.is_file():
                    file_time = datetime.fromtimestamp(file_path.stat().st_mtime)
                    if file_time < cutoff_time:
                        file_path.unlink()
                        cleaned_count += 1

            logger.info(f"Cleaned up {cleaned_count} temporary report files")
            return cleaned_count

        except Exception as e:
            logger.error(f"Error cleaning up temp files: {e}")
            return 0

    def __del__(self):
        """Cleanup on service destruction"""
        if hasattr(self, "executor"):
            self.executor.shutdown(wait=False)


# Global instance
report_service = ReportGenerationService()
