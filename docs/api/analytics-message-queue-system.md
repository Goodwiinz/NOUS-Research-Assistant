# Analytics Dashboard - Message Queue System for Async Processing

> **Stale / historical (2026-10-17):** It covers only part of the API and its paths and schemas may have drifted. The source of truth is the generated `backend/openapi.json` (also served at `/docs` when `DEBUG` is on). Current contracts: [`docs/engineering/`](../engineering/README.md).

## 1. Message Queue Architecture Overview

### Queue System Topology

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          Message Broker Cluster                             │
│                                                                             │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐           │
│  │   RabbitMQ      │  │   Redis Streams │  │   Apache Kafka  │           │
│  │   (Primary)     │  │   (Real-time)   │  │   (Event Log)   │           │
│  │                 │  │                 │  │                 │           │
│  │ - Task Queues   │  │ - Stream        │  │ - Event Store   │           │
│  │ - RPC Pattern   │  │   Processing    │  │ - Log Compaction│           │
│  │ - Routing Keys  │  │ - Pub/Sub       │  │ - Replay        │           │
│  │ - Dead Letter   │  │ - Consumer      │  │ - Partitions    │           │
│  │   Exchange      │  │   Groups        │  │ - Offsets       │           │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘           │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
          ┌───────────────────────────┼───────────────────────────┐
          │                           │                           │
          ▼                           ▼                           ▼
┌─────────────────┐      ┌─────────────────┐      ┌─────────────────┐
│  Task Producers │      │ Event Publishers │      │  API Gateway    │
│                 │      │                 │      │                 │
│ - REST APIs     │      │ - Database      │      │ - HTTP Requests │
│ - WebSocket     │      │   Triggers      │      │ - GraphQL       │
│ - Scheduled     │      │ - File Watchers │      │ - Webhooks      │
│   Jobs          │      │ - External      │      │ - CLI Tools     │
│ - UI Events     │      │   Integrations  │      │                 │
└─────────────────┘      └─────────────────┘      └─────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           Message Consumers                                 │
│                                                                             │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐           │
│  │   Graph         │  │   Report        │  │   Real-time     │           │
│  │   Analytics     │  │   Generation    │  │   Processing    │           │
│  │   Workers       │  │   Workers       │  │   Workers       │           │
│  │                 │  │                 │  │                 │           │
│  │ - Centrality    │  │ - PDF Export    │  │ - Metric        │           │
│  │ - Clustering    │  │ - CSV Export    │  │   Aggregation   │           │
│  │ - Path Analysis │  │ - Email Reports │  │ - Alert         │           │
│  │ - Community     │  │ - Schedule      │  │   Processing    │           │
│  │   Detection     │  │   Processing    │  │ - Cache         │           │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘           │
│                                                                             │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐           │
│  │   Data          │  │   Notification  │  │   Maintenance   │           │
│  │   Processing    │  │   Workers       │  │   Workers       │           │
│  │   Workers       │  │                 │  │                 │           │
│  │                 │  │ - Email Sender  │  │ - Cache Cleanup │           │
│  │ - ETL Jobs      │  │ - Slack/Teams   │  │ - Log Rotation  │           │
│  │ - Aggregation   │  │ - Webhooks      │  │ - Data Archive  │           │
│  │ - Validation    │  │ - SMS Gateway   │  │ - Health Checks │           │
│  │ - Enrichment    │  │ - Push          │  │ - Backup Tasks  │           │
│  │                 │  │   Notifications │  │                 │           │
│  └─────────────────┘  └─────────────────┘  └─────────────────┘           │
└─────────────────────────────────────────────────────────────────────────────┘
```

## 2. Queue Types and Use Cases

### 2.1 Task Queues (RabbitMQ)

#### Graph Analytics Queue
```python
# RabbitMQ Configuration for Graph Analytics
GRAPH_ANALYTICS_QUEUE = {
    "name": "analytics.graph.tasks",
    "durable": True,
    "auto_delete": False,
    "arguments": {
        "x-message-ttl": 3600000,  # 1 hour
        "x-max-length": 10000,      # Max 10k messages
        "x-dead-letter-exchange": "analytics.graph.dlx",
        "x-dead-letter-routing-key": "failed"
    }
}

class GraphAnalyticsTask:
    """Graph analytics task message structure"""

    def __init__(self, task_type: str, organization_id: str, parameters: dict):
        self.task_id = str(uuid.uuid4())
        self.task_type = task_type  # centrality, clustering, paths
        self.organization_id = organization_id
        self.parameters = parameters
        self.created_at = datetime.utcnow()
        self.priority = self._calculate_priority()
        self.retry_count = 0
        self.max_retries = 3

    def to_message(self) -> dict:
        return {
            "task_id": self.task_id,
            "task_type": self.task_type,
            "organization_id": self.organization_id,
            "parameters": self.parameters,
            "created_at": self.created_at.isoformat(),
            "priority": self.priority,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries
        }

    def _calculate_priority(self) -> int:
        """Calculate task priority based on parameters"""
        if self.task_type == "centrality":
            return 5  # High priority
        elif self.task_type == "clustering":
            return 3  # Medium priority
        elif self.task_type == "paths":
            return 7  # Very high priority
        return 1  # Low priority
```

#### Report Generation Queue
```python
REPORT_GENERATION_QUEUE = {
    "name": "analytics.reports.tasks",
    "durable": True,
    "auto_delete": False,
    "arguments": {
        "x-message-ttl": 7200000,  # 2 hours
        "x-max-length": 5000,
        "x-dead-letter-exchange": "analytics.reports.dlx",
        "x-dead-letter-routing-key": "failed"
    }
}

class ReportGenerationTask:
    """Report generation task message structure"""

    def __init__(
        self,
        report_id: str,
        organization_id: str,
        execution_type: str,
        parameters: dict,
        output_formats: List[str]
    ):
        self.execution_id = str(uuid.uuid4())
        self.report_id = report_id
        self.organization_id = organization_id
        self.execution_type = execution_type  # manual, scheduled, api
        self.parameters = parameters
        self.output_formats = output_formats
        self.created_at = datetime.utcnow()
        self.expires_at = datetime.utcnow() + timedelta(hours=2)

    def to_message(self) -> dict:
        return {
            "execution_id": self.execution_id,
            "report_id": self.report_id,
            "organization_id": self.organization_id,
            "execution_type": self.execution_type,
            "parameters": self.parameters,
            "output_formats": self.output_formats,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat()
        }
```

### 2.2 Event Streams (Redis Streams)

#### Real-time Metrics Stream
```python
class RealtimeMetricsStream:
    """Redis Stream for real-time metrics processing"""

    STREAM_KEY = "analytics:realtime:metrics"
    CONSUMER_GROUP = "metrics_processors"

    async def publish_metric_update(
        self,
        organization_id: str,
        metric_type: str,
        value: float,
        metadata: dict = None
    ):
        """Publish a metric update to the stream"""

        message = {
            "organization_id": organization_id,
            "metric_type": metric_type,
            "value": value,
            "timestamp": datetime.utcnow().isoformat(),
            "metadata": metadata or {}
        }

        # Add to stream with ID = timestamp
        message_id = await self.redis.xadd(
            self.STREAM_KEY,
            message,
            maxlen=10000,  # Keep last 10k messages
            approximate=True
        )

        return message_id

    async def consume_metrics(self, processor_id: str):
        """Consume metrics from the stream"""

        try:
            # Create consumer group if it doesn't exist
            await self.redis.xgroup_create(
                self.STREAM_KEY,
                self.CONSUMER_GROUP,
                id="0",
                mkstream=True
            )
        except redis.ResponseError:
            pass  # Group already exists

        while True:
            try:
                # Read messages for this consumer
                messages = await self.redis.xreadgroup(
                    self.CONSUMER_GROUP,
                    processor_id,
                    {self.STREAM_KEY: ">"},  # Only new messages
                    count=10,
                    block=1000  # Block for 1 second
                )

                for stream, msgs in messages:
                    for message_id, fields in msgs:
                        await self._process_metric_message(
                            message_id, fields, processor_id
                        )

            except Exception as e:
                logger.error(f"Error consuming metrics: {e}")
                await asyncio.sleep(5)

    async def _process_metric_message(
        self,
        message_id: str,
        fields: dict,
        processor_id: str
    ):
        """Process a single metric message"""

        try:
            organization_id = fields["organization_id"]
            metric_type = fields["metric_type"]
            value = float(fields["value"])
            timestamp = fields["timestamp"]

            # Update real-time cache
            await self.update_realtime_cache(
                organization_id, metric_type, value, timestamp
            )

            # Check for alert conditions
            await self.check_alert_conditions(
                organization_id, metric_type, value
            )

            # Acknowledge message processing
            await self.redis.xack(
                self.STREAM_KEY,
                self.CONSUMER_GROUP,
                message_id
            )

        except Exception as e:
            logger.error(f"Error processing message {message_id}: {e}")
            # Don't acknowledge - message will be retried
```

#### Analytics Events Stream
```python
class AnalyticsEventsStream:
    """Redis Stream for analytics events"""

    STREAM_KEY = "analytics:events"
    CONSUMER_GROUP = "event_processors"

    async def publish_entity_event(
        self,
        event_type: str,
        organization_id: str,
        entity_data: dict
    ):
        """Publish entity-related event"""

        message = {
            "event_type": event_type,  # created, updated, deleted
            "organization_id": organization_id,
            "entity_type": entity_data.get("entity_type"),
            "entity_id": entity_data.get("entity_id"),
            "timestamp": datetime.utcnow().isoformat(),
            "data": entity_data
        }

        return await self.redis.xadd(self.STREAM_KEY, message)

    async def publish_relationship_event(
        self,
        event_type: str,
        organization_id: str,
        relationship_data: dict
    ):
        """Publish relationship-related event"""

        message = {
            "event_type": event_type,
            "organization_id": organization_id,
            "relationship_type": relationship_data.get("relationship_type"),
            "source_id": relationship_data.get("source_id"),
            "target_id": relationship_data.get("target_id"),
            "timestamp": datetime.utcnow().isoformat(),
            "data": relationship_data
        }

        return await self.redis.xadd(self.STREAM_KEY, message)
```

### 2.3 Event Log (Apache Kafka)

#### Analytics Events Topic
```python
# Kafka Topics Configuration
KAFKA_TOPICS = {
    "analytics-events": {
        "partitions": 6,
        "replication_factor": 3,
        "retention_ms": 7 * 24 * 60 * 60 * 1000,  # 7 days
        "segment_ms": 24 * 60 * 60 * 1000,         # 1 day segments
        "cleanup_policy": "delete"
    },
    "user-interactions": {
        "partitions": 12,
        "replication_factor": 3,
        "retention_ms": 30 * 24 * 60 * 60 * 1000,  # 30 days
        "segment_ms": 24 * 60 * 60 * 1000,
        "cleanup_policy": "compact,delete"
    },
    "system-metrics": {
        "partitions": 3,
        "replication_factor": 3,
        "retention_ms": 3 * 24 * 60 * 60 * 1000,   # 3 days
        "segment_ms": 6 * 60 * 60 * 1000,          # 6 hours segments
        "cleanup_policy": "delete"
    }
}

class AnalyticsEventProducer:
    """Kafka producer for analytics events"""

    def __init__(self):
        self.producer = KafkaProducer(
            bootstrap_servers=['kafka1:9092', 'kafka2:9092', 'kafka3:9092'],
            value_serializer=lambda v: json.dumps(v).encode('utf-8'),
            key_serializer=lambda k: k.encode('utf-8') if k else None,
            acks='all',
            retries=3,
            batch_size=16384,
            linger_ms=10,
            buffer_memory=33554432
        )

    async def publish_dashboard_event(
        self,
        event_type: str,
        organization_id: str,
        user_id: str,
        event_data: dict
    ):
        """Publish dashboard interaction event"""

        event = {
            "event_id": str(uuid.uuid4()),
            "event_type": event_type,  # view, edit, share, export
            "organization_id": organization_id,
            "user_id": user_id,
            "timestamp": datetime.utcnow().isoformat(),
            "data": event_data,
            "source": "analytics_dashboard"
        }

        # Use organization_id as partition key for data locality
        future = self.producer.send(
            'analytics-events',
            key=organization_id,
            value=event
        )

        # Block for confirmation in production
        record_metadata = future.get(timeout=10)

        return {
            "topic": record_metadata.topic,
            "partition": record_metadata.partition,
            "offset": record_metadata.offset
        }

    async def publish_user_interaction(
        self,
        organization_id: str,
        user_id: str,
        interaction_data: dict
    ):
        """Publish detailed user interaction event"""

        event = {
            "event_id": str(uuid.uuid4()),
            "organization_id": organization_id,
            "user_id": user_id,
            "session_id": interaction_data.get("session_id"),
            "interaction_type": interaction_data.get("type"),  # click, hover, scroll
            "component": interaction_data.get("component"),
            "timestamp": datetime.utcnow().isoformat(),
            "data": interaction_data,
            "user_agent": interaction_data.get("user_agent"),
            "ip_address": interaction_data.get("ip_address")
        }

        # Use user_id as partition key for user-specific analysis
        future = self.producer.send(
            'user-interactions',
            key=user_id,
            value=event
        )

        return future.get(timeout=10)
```

## 3. Worker Implementation

### 3.1 Graph Analytics Worker
```python
class GraphAnalyticsWorker:
    """Worker for processing graph analytics tasks"""

    def __init__(self, queue_connection: pika.BlockingConnection):
        self.connection = queue_connection
        self.channel = self.connection.channel()
        self.neo4j_driver = Neo4jDriver()
        self.redis_client = RedisConnection()

        self.setup_queue()

    def setup_queue(self):
        """Setup RabbitMQ queue and consumer"""

        # Declare queue
        self.channel.queue_declare(**GRAPH_ANALYTICS_QUEUE)

        # Declare dead letter exchange
        self.channel.exchange_declare(
            exchange="analytics.graph.dlx",
            exchange_type="direct",
            durable=True
        )

        # Bind dead letter queue
        self.channel.queue_declare(
            queue="analytics.graph.failed",
            durable=True
        )
        self.channel.queue_bind(
            exchange="analytics.graph.dlx",
            queue="analytics.graph.failed",
            routing_key="failed"
        )

        # Set QoS for fair dispatch
        self.channel.basic_qos(prefetch_count=1)

    def start_consuming(self):
        """Start consuming messages from the queue"""

        self.channel.basic_consume(
            queue=GRAPH_ANALYTICS_QUEUE["name"],
            on_message_callback=self.process_task,
            auto_ack=False
        )

        logger.info("Graph Analytics Worker started consuming messages")
        self.channel.start_consuming()

    def process_task(self, ch, method, properties, body):
        """Process a single graph analytics task"""

        try:
            task_data = json.loads(body)
            task = GraphAnalyticsTask.from_message(task_data)

            logger.info(f"Processing graph analytics task: {task.task_id}")

            # Route to appropriate processor
            if task.task_type == "centrality":
                result = self.process_centrality_task(task)
            elif task.task_type == "clustering":
                result = self.process_clustering_task(task)
            elif task.task_type == "paths":
                result = self.process_paths_task(task)
            else:
                raise ValueError(f"Unknown task type: {task.task_type}")

            # Cache result
            await self.cache_task_result(task, result)

            # Publish completion event
            await self.publish_completion_event(task, result)

            # Acknowledge message
            ch.basic_ack(delivery_tag=method.delivery_tag)

            logger.info(f"Completed graph analytics task: {task.task_id}")

        except Exception as e:
            logger.error(f"Error processing task: {e}")

            # Check if we should retry
            if task.retry_count < task.max_retries:
                # Requeue with delay
                task.retry_count += 1
                self.requeue_task_with_delay(task)
                ch.basic_ack(delivery_tag=method.delivery_tag)
            else:
                # Send to dead letter queue
                ch.basic_nack(
                    delivery_tag=method.delivery_tag,
                    requeue=False
                )

    def process_centrality_task(self, task: GraphAnalyticsTask) -> dict:
        """Process centrality analysis task"""

        algorithm = task.parameters["algorithm"]
        entity_types = task.parameters.get("entity_types", [])
        organization_id = task.organization_id

        with self.neo4j_driver.session() as session:
            # Build Cypher query based on algorithm
            if algorithm == "degree":
                query = """
                MATCH (n:Entity)
                WHERE n.organization_id = $org_id
                $entity_type_filter
                WITH n, size((n)--()) as degree
                RETURN n.entity_id as entity_id,
                       n.name as entity_name,
                       n.entity_type as entity_type,
                       degree as centrality_score
                ORDER BY degree DESC
                LIMIT $limit
                """
            elif algorithm == "betweenness":
                # Use graph algorithms library
                query = """
                CALL gds.betweenness.stream({
                    nodeProjection: {
                        Entity: {
                            label: 'Entity',
                            properties: ['entity_id', 'name', 'entity_type']
                        }
                    },
                    relationshipProjection: {
                        RELATIONSHIP: {
                            type: '*',
                            orientation: 'UNDIRECTED'
                        }
                    },
                    nodeLabels: ['Entity'],
                    relationshipTypes: ['*'],
                    sourceNode: null,
                    targetNode: null
                })
                YIELD nodeId, score
                RETURN gds.util.asNode(nodeId).entity_id as entity_id,
                       gds.util.asNode(nodeId).name as entity_name,
                       gds.util.asNode(nodeId).entity_type as entity_type,
                       score as centrality_score
                ORDER BY score DESC
                LIMIT $limit
                """

            # Execute query
            params = {
                "org_id": organization_id,
                "limit": task.parameters.get("limit", 100)
            }

            if entity_types:
                params["entity_type_filter"] = f"AND n.entity_type IN {entity_types}"
                query = query.replace("$entity_type_filter", "AND n.entity_type IN $entity_types")
            else:
                query = query.replace("$entity_type_filter", "")

            result = session.run(query, params)

            centrality_scores = [
                {
                    "entity_id": record["entity_id"],
                    "entity_name": record["entity_name"],
                    "entity_type": record["entity_type"],
                    "centrality_score": record["centrality_score"]
                }
                for record in result
            ]

            return {
                "algorithm": algorithm,
                "computed_at": datetime.utcnow().isoformat(),
                "total_entities": len(centrality_scores),
                "results": centrality_scores
            }
```

### 3.2 Report Generation Worker
```python
class ReportGenerationWorker:
    """Worker for processing report generation tasks"""

    def __init__(self, queue_connection: pika.BlockingConnection):
        self.connection = queue_connection
        self.channel = self.connection.channel()
        self.db_session = DatabaseSession()
        self.template_engine = Jinja2Templates()

        self.setup_queue()

    def process_report_task(self, ch, method, properties, body):
        """Process a single report generation task"""

        try:
            task_data = json.loads(body)
            task = ReportGenerationTask.from_message(task_data)

            logger.info(f"Processing report generation task: {task.execution_id}")

            # Update execution status
            await self.update_execution_status(
                task.execution_id,
                "running",
                started_at=datetime.utcnow()
            )

            # Generate report data
            report_data = await self.generate_report_data(task)

            # Generate output files
            output_files = {}
            for format_type in task.output_formats:
                output_file = await self.generate_output_format(
                    report_data, format_type, task
                )
                output_files[format_type] = output_file

            # Update execution with results
            await self.update_execution_status(
                task.execution_id,
                "completed",
                completed_at=datetime.utcnow(),
                result_files=output_files
            )

            # Send notifications if configured
            await self.send_completion_notifications(task, output_files)

            # Acknowledge message
            ch.basic_ack(delivery_tag=method.delivery_tag)

        except Exception as e:
            logger.error(f"Error processing report task: {e}")

            # Update execution status with error
            await self.update_execution_status(
                task.execution_id,
                "failed",
                error_message=str(e),
                completed_at=datetime.utcnow()
            )

            # Send to dead letter queue
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

    async def generate_output_format(
        self,
        report_data: dict,
        format_type: str,
        task: ReportGenerationTask
    ) -> dict:
        """Generate report in specified format"""

        if format_type == "pdf":
            return await self.generate_pdf_report(report_data, task)
        elif format_type == "csv":
            return await self.generate_csv_report(report_data, task)
        elif format_type == "json":
            return await self.generate_json_report(report_data, task)
        elif format_type == "xlsx":
            return await self.generate_excel_report(report_data, task)
        else:
            raise ValueError(f"Unsupported output format: {format_type}")

    async def generate_pdf_report(
        self,
        report_data: dict,
        task: ReportGenerationTask
    ) -> dict:
        """Generate PDF report"""

        # Render HTML template
        html_content = await self.template_engine.render_template(
            "report_template.html",
            data=report_data,
            organization_id=task.organization_id,
            generated_at=datetime.utcnow()
        )

        # Convert to PDF
        pdf_file = await self.html_to_pdf(html_content)

        # Store file
        file_path = f"reports/{task.execution_id}/report.pdf"
        await self.storage_service.store_file(file_path, pdf_file)

        return {
            "format": "pdf",
            "file_path": file_path,
            "file_size": len(pdf_file),
            "content_type": "application/pdf"
        }
```

## 4. Message Patterns

### 4.1 Request-Reply Pattern
```python
class RPCClient:
    """RPC client for synchronous requests"""

    def __init__(self, connection):
        self.connection = connection
        self.channel = connection.channel()

        # Setup callback queue
        result = self.channel.queue_declare(queue='', exclusive=True)
        self.callback_queue = result.method.queue

        self.channel.basic_consume(
            queue=self.callback_queue,
            on_message_callback=self.on_response,
            auto_ack=True
        )

        self.response = None
        self.corr_id = None

    def on_response(self, ch, method, props, body):
        """Handle RPC response"""
        if self.corr_id == props.correlation_id:
            self.response = json.loads(body)

    def call(self, routing_key: str, message: dict, timeout: int = 30) -> dict:
        """Make synchronous RPC call"""

        self.response = None
        self.corr_id = str(uuid.uuid4())

        # Send request
        self.channel.basic_publish(
            exchange='',
            routing_key=routing_key,
            properties=pika.BasicProperties(
                reply_to=self.callback_queue,
                correlation_id=self.corr_id,
            ),
            body=json.dumps(message)
        )

        # Wait for response
        start_time = time.time()
        while self.response is None:
            if time.time() - start_time > timeout:
                raise TimeoutError("RPC call timed out")
            self.connection.process_data_events()

        return self.response

# Usage example
rpc_client = RPCClient(connection)
try:
    result = rpc_client.call(
        "analytics.graph.rpc",
        {
            "task_type": "centrality",
            "organization_id": "org-123",
            "parameters": {"algorithm": "pagerank", "limit": 10}
        },
        timeout=60
    )
    print(f"Centrality result: {result}")
except TimeoutError:
    print("Graph analytics computation timed out")
```

### 4.2 Fan-out Pattern
```python
class EventFanout:
    """Fan-out pattern for broadcasting events"""

    def __init__(self, connection):
        self.connection = connection
        self.channel = connection.channel()

        # Declare fanout exchange
        self.channel.exchange_declare(
            exchange='analytics.events.fanout',
            exchange_type='fanout',
            durable=True
        )

    async def broadcast_event(self, event_type: str, event_data: dict):
        """Broadcast event to all subscribers"""

        message = {
            "event_id": str(uuid.uuid4()),
            "event_type": event_type,
            "timestamp": datetime.utcnow().isoformat(),
            "data": event_data
        }

        self.channel.basic_publish(
            exchange='analytics.events.fanout',
            routing_key='',
            body=json.dumps(message),
            properties=pika.BasicProperties(
                delivery_mode=2,  # Persistent message
                content_type='application/json'
            )
        )

        logger.info(f"Broadcasted event: {event_type}")

# Event subscribers
class EventSubscriber:
    """Subscribe to specific events"""

    def __init__(self, connection, queue_name: str):
        self.connection = connection
        self.channel = connection.channel()
        self.queue_name = queue_name

        # Create exclusive queue for this subscriber
        self.channel.queue_declare(queue=queue_name, exclusive=True)

        # Bind to fanout exchange
        self.channel.queue_bind(
            exchange='analytics.events.fanout',
            queue=queue_name
        )

    def start_consuming(self, callback):
        """Start consuming events"""

        self.channel.basic_consume(
            queue=self.queue_name,
            on_message_callback=callback,
            auto_ack=True
        )

        self.channel.start_consuming()
```

## 5. Queue Monitoring and Management

### 5.1 Queue Health Monitoring
```python
class QueueMonitor:
    """Monitor queue health and performance"""

    def __init__(self):
        self.rabbitmq_connection = RabbitMQConnection()
        self.redis_client = RedisConnection()
        self.kafka_admin = KafkaAdminClient()

    async def get_queue_health(self) -> dict:
        """Get comprehensive queue health metrics"""

        health_metrics = {
            "rabbitmq": await self.get_rabbitmq_health(),
            "redis_streams": await self.get_redis_streams_health(),
            "kafka": await self.get_kafka_health()
        }

        return health_metrics

    async def get_rabbitmq_health(self) -> dict:
        """Get RabbitMQ queue health"""

        channel = self.rabbitmq_connection.channel()

        # Get queue stats
        queues_to_monitor = [
            "analytics.graph.tasks",
            "analytics.reports.tasks",
            "analytics.notifications.tasks"
        ]

        queue_stats = {}
        for queue_name in queues_to_monitor:
            try:
                method = channel.queue_declare(queue=queue_name, passive=True)
                queue_stats[queue_name] = {
                    "message_count": method.method.message_count,
                    "consumer_count": method.method.consumer_count,
                    "status": "healthy"
                }
            except Exception as e:
                queue_stats[queue_name] = {
                    "status": "error",
                    "error": str(e)
                }

        # Get connection info
        connection_info = self.rabbitmq_connection.get_connection_info()

        return {
            "queues": queue_stats,
            "connections": connection_info,
            "overall_status": "healthy" if all(
                q["status"] == "healthy" for q in queue_stats.values()
            ) else "degraded"
        }

    async def get_redis_streams_health(self) -> dict:
        """Get Redis streams health"""

        streams_to_monitor = [
            "analytics:realtime:metrics",
            "analytics:events",
            "analytics:alerts"
        ]

        stream_stats = {}
        for stream_key in streams_to_monitor:
            try:
                info = self.redis_client.xinfo_stream(stream_key)
                groups = self.redis_client.xinfo_groups(stream_key)

                stream_stats[stream_key] = {
                    "length": info["length"],
                    "groups": len(groups),
                    "last_generated_id": info["last-generated-id"],
                    "max_deleted_entry_id": info.get("max-deleted-entry-id", "0"),
                    "status": "healthy"
                }
            except Exception as e:
                stream_stats[stream_key] = {
                    "status": "error",
                    "error": str(e)
                }

        return {
            "streams": stream_stats,
            "overall_status": "healthy" if all(
                s["status"] == "healthy" for s in stream_stats.values()
            ) else "degraded"
        }
```

### 5.2 Queue Management API
```python
class QueueManager:
    """Administrative operations on queues"""

    def __init__(self):
        self.rabbitmq_connection = RabbitMQConnection()
        self.redis_client = RedisConnection()

    async def purge_queue(self, queue_name: str) -> dict:
        """Purge all messages from a queue"""

        try:
            channel = self.rabbitmq_connection.channel()
            method = channel.queue_purge(queue=queue_name)

            return {
                "queue": queue_name,
                "messages_purged": method.method.message_count,
                "status": "success"
            }
        except Exception as e:
            return {
                "queue": queue_name,
                "status": "error",
                "error": str(e)
            }

    async def replay_failed_messages(
        self,
        queue_name: str,
        max_messages: int = 100
    ) -> dict:
        """Replay messages from dead letter queue"""

        dlq_queue = f"{queue_name}.failed"

        try:
            channel = self.rabbitmq_connection.channel()

            # Get messages from DLQ
            method_frame, header_frame, body = channel.basic_get(queue=dlq_queue)

            replayed_count = 0
            while method_frame and replayed_count < max_messages:
                # Parse and update message
                message = json.loads(body)
                message["retry_count"] = message.get("retry_count", 0) + 1

                # Re-publish to original queue
                channel.basic_publish(
                    exchange='',
                    routing_key=queue_name,
                    body=json.dumps(message),
                    properties=pika.BasicProperties(
                        delivery_mode=2
                    )
                )

                # Acknowledge DLQ message
                channel.basic_ack(delivery_tag=method_frame.delivery_tag)

                replayed_count += 1

                if replayed_count < max_messages:
                    method_frame, header_frame, body = channel.basic_get(queue=dlq_queue)

            return {
                "queue": queue_name,
                "dlq_queue": dlq_queue,
                "messages_replayed": replayed_count,
                "status": "success"
            }

        except Exception as e:
            return {
                "queue": queue_name,
                "status": "error",
                "error": str(e)
            }
```

## 6. Implementation Checklist

### Message Queue Implementation Tasks

- [ ] RabbitMQ cluster setup with high availability
- [ ] Redis Streams configuration for real-time processing
- [ ] Apache Kafka setup for event logging
- [ ] Queue topology design and configuration
- [ ] Worker implementations for all task types
- [ ] Message serialization and validation
- [ ] Dead letter queue handling
- [ ] Message retry and error handling
- [ ] Queue monitoring and alerting
- [ ] Performance optimization and tuning
- [ ] Security and access control
- [ ] Backup and disaster recovery

### Monitoring and Management

- [ ] Queue depth monitoring
- [ ] Message processing latency tracking
- [ ] Worker health monitoring
- [ ] Dead letter queue monitoring
- [ ] Consumer lag monitoring (Kafka)
- [ ] Throughput and performance metrics
- [ ] Error rate and failure analysis

This comprehensive message queue system design ensures reliable asynchronous processing for the Knowledge Graph Analytics Dashboard with proper handling of compute-intensive tasks, real-time data processing, and event-driven workflows.