import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import { Protocol } from 'aws-cdk-lib/aws-ecs';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as assets from 'aws-cdk-lib/aws-ecr-assets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';

import { DockerImageAsset } from "aws-cdk-lib/aws-ecr-assets";
import * as path from 'path';
import * as fs from 'fs';
import * as dotenv from 'dotenv';


export class FantasyFootballAgentStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // read .env variables into map
    const envPath = path.join(__dirname, '../../.env');
    if (!fs.existsSync(envPath)) {
      throw new Error('.env file not found!');
    }

    const envConfig = dotenv.parse(fs.readFileSync(envPath));
    const dotenvMap = Object.entries(envConfig).reduce((acc, [key, value]) => {
      acc[key] = value;
      return acc;
    }, {} as { [key: string]: string });

    // basic vpc
    const vpc = new ec2.Vpc(this, 'AgentVPC', {
      maxAzs: 2,  
      natGateways: 1 
    });

    const serviceSecurityGroup = new ec2.SecurityGroup(this, 'serviceSecurityGroup', {
      allowAllOutbound: true,
      vpc: vpc,
    });

    // ecs cluster
    const cluster = new ecs.Cluster(this, 'AgentCluster', {
      vpc,
      clusterName: 'fantasy-football-agent-cluster',
      containerInsights: true 
    });

    const executionRole = new iam.Role(this, 'executionRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      description: 'Execution role for ECS tasks',
    });
    executionRole.addManagedPolicy(
      iam.ManagedPolicy.fromAwsManagedPolicyName('service-role/AmazonECSTaskExecutionRolePolicy')
    );
    executionRole.addToPolicy(
      new iam.PolicyStatement({
        effect: iam.Effect.ALLOW,
        actions: [
          'logs:CreateLogStream',
          'logs:PutLogEvents',
          'logs:CreateLogGroup'
        ],
        resources: ['*'] 
      })
    );

    const taskDefinition = new ecs.FargateTaskDefinition(this, 'TaskDef', {
      executionRole: executionRole,
      memoryLimitMiB: 4096,
      cpu: 1024,
    });
    
    // bedrock model access
    const bedrockInvokePolicy = new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: [
        'bedrock:InvokeModel',
        'bedrock:InvokeModelWithResponseStream'
      ],
      resources: ['arn:aws:bedrock:*:*:model/*']
    });
    taskDefinition.addToTaskRolePolicy(bedrockInvokePolicy);

    const postgresContainer = taskDefinition.addContainer('langgraph-postgres', {
      containerName: 'langgraph-postgres',
      image: ecs.ContainerImage.fromRegistry('postgres:16'),
      portMappings: [{
        containerPort: 5432,
      }],
      environment: {
        POSTGRES_DB: 'postgres',
        POSTGRES_USER: 'postgres',
        POSTGRES_PASSWORD: 'postgres',
      },
      healthCheck: {
        command: ['pg_isready', '-U', 'postgres'],
        interval: cdk.Duration.seconds(5),
        startPeriod: cdk.Duration.seconds(10),
        timeout: cdk.Duration.seconds(5),
        retries: 5,
      },
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'langgraph-postgres',
      }),
    });

    taskDefinition.addVolume({
      name: 'langgraph-data',
    });

    // Mount the volume to PostgreSQL container
    postgresContainer.addMountPoints({
      sourceVolume: 'langgraph-data',
      containerPath: '/var/lib/postgresql/data',
      readOnly: false,
    });

    const redisContainer = taskDefinition.addContainer('langgraph-redis', {
      containerName: 'langgraph-redis',
      image: ecs.ContainerImage.fromRegistry('redis:6'),
      portMappings: [{
        containerPort: 6379,
      }],
      healthCheck: {
        command: ['redis-cli', 'ping'],
        interval: cdk.Duration.seconds(5),
        timeout: cdk.Duration.seconds(5),
        retries: 5,
      },
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'langgraph-redis',
      }),
    });

    // networking within the same ECS service exposes each container at localhost
    const apiImageAsset = new DockerImageAsset(this, 'apiImageAsset', {
      directory: path.join(__dirname, '../../fantasy_chatbot'),
      file: 'api.Dockerfile',
      platform: assets.Platform.LINUX_AMD64,
    });
    const apiContainer = taskDefinition.addContainer('langgraph-api', {
      containerName: 'langgraph-api',
      image: ecs.ContainerImage.fromDockerImageAsset(apiImageAsset),
      portMappings: [{
        containerPort: 8000,
      }],
      environment: {
        ...dotenvMap,
        REDIS_URI: 'redis://127.0.0.1:6379',
        POSTGRES_URI: 'postgres://postgres:postgres@127.0.0.1:5432/postgres?sslmode=disable'
      },
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'langgraph-api',
      }),
    });

    apiContainer.addContainerDependencies(
      {
        container: redisContainer,
        condition: ecs.ContainerDependencyCondition.HEALTHY,
      },
      {
        container: postgresContainer,
        condition: ecs.ContainerDependencyCondition.HEALTHY,
      },
    );

    const appImageAsset = new DockerImageAsset(this, 'appImageAsset', {
      directory: path.join(__dirname, '../../fantasy_chatbot'),
      file: 'app.Dockerfile',
      platform: assets.Platform.LINUX_AMD64,
    });
    const appContainer = taskDefinition.addContainer('langgraph-app', {
      containerName: 'langgraph-app',
      image: ecs.ContainerImage.fromDockerImageAsset(appImageAsset),
      portMappings: [{
        containerPort: 8501,
        protocol: Protocol.TCP,
      }],
      environment: {
        LANGGRAPH_API_URL: 'http://127.0.0.1:8000',
      },
      healthCheck: {
        command: ['curl', 'http://localhost:8501/_stcore/health'],
        startPeriod: cdk.Duration.seconds(15),
        interval: cdk.Duration.seconds(5),
        timeout: cdk.Duration.seconds(5),
        retries: 5,
      },
      logging: ecs.LogDrivers.awsLogs({
        streamPrefix: 'langgraph-app',
      }),
    });

    const appService = new ecs.FargateService(this, 'appService', {
      taskDefinition: taskDefinition,
      cluster: cluster,
      securityGroups: [serviceSecurityGroup],
      assignPublicIp: true,
      desiredCount: 1,
      vpcSubnets: vpc.selectSubnets({subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS}),
    });

    // security group for the ALB
    const albSecurityGroup = new ec2.SecurityGroup(this, 'AlbSecurityGroup', {
      vpc,
      allowAllOutbound: true,
      description: 'Security group for ALB'
    });

    const alb = new elbv2.ApplicationLoadBalancer(this, 'AgentALB', {
      vpc,
      internetFacing: true,
      loadBalancerName: 'fantasy-football-alb',
      securityGroup: albSecurityGroup,
      vpcSubnets: vpc.selectSubnets({subnetType: ec2.SubnetType.PUBLIC}),
    });

    albSecurityGroup.addIngressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(80),
      'Allow HTTP traffic'
    );

    serviceSecurityGroup.addIngressRule(
      albSecurityGroup,
      ec2.Port.tcp(8501),
      'Allow traffic from ALB to application port'
    );

    serviceSecurityGroup.addIngressRule(
      serviceSecurityGroup,
      ec2.Port.allTcp(),
      'Allow internal traffic'
    );

    const listener = alb.addListener('Listener', {
      port: 80,
    });
    listener.addTargets('AgentTarget', {
      port: 8501,
      targets: [
          // explicitly mention the particular container
          appService.loadBalancerTarget({
            containerName: appContainer.containerName,
            containerPort: appContainer.containerPort,
          })
      ],
      healthCheck: {
        path: '/_stcore/health',
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
        unhealthyThresholdCount: 5,
        port: '8501' 
      },
      protocol: elbv2.ApplicationProtocol.HTTP
    });


    // output the ALB DNS name
    new cdk.CfnOutput(this, 'LoadBalancerDNS', {
      value: `http://${alb.loadBalancerDnsName}`,
      description: 'Load balancer DNS'
    });

  }
}
