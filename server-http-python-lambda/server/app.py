from awslabs.mcp_lambda_handler import MCPLambdaHandler
from datetime import datetime, UTC
import random
import boto3
import os
import ipaddress
import json

# Get session table name from environment variable
session_table = os.environ.get('MCP_SESSION_TABLE', 'mcp_sessions')

# Create the MCP server instance
mcp_server = MCPLambdaHandler(name="mcp-lambda-server", version="1.0.0", session_store=session_table)

@mcp_server.tool()
def get_weather(city: str) -> str:
    """Get the current weather for a city.
    
    Args:
        city: Name of the city to get weather for
        
    Returns:
        A string describing the weather
    """
    temp = random.randint(15, 35)
    return f"The temperature in {city} is {temp}°C"

@mcp_server.tool()
def count_s3_buckets() -> int:
    """Count the number of S3 buckets."""
    s3 = boto3.client('s3')
    response = s3.list_buckets()
    return len(response['Buckets'])

@mcp_server.tool()
def get_time() -> str:
    """Get the current UTC date and time, and show how long since last time was asked for (if session available)."""
    try:
        # Get the current UTC time as a datetime object and formatted string
        now = datetime.now(UTC)
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")

        # Retrieve the session object if available
        session = mcp_server.get_session()
        # Get the last time the user asked for the time from the session (if any)
        last_time = session.get('last_time_asked') if session else None
        last_time_str = last_time if last_time else None
        seconds_since = None

        # If there was a previous time, calculate how many seconds have passed
        if last_time:
            try:
                last_time_dt = datetime.strptime(last_time, "%Y-%m-%d %H:%M:%S")
                seconds_since = int((now - last_time_dt).total_seconds())
            except Exception:
                # If parsing fails, just skip the seconds_since calculation
                seconds_since = None

        # Store the current time as the new 'last_time_asked' in the session
        if session:
            def update_last_time(s):
                s.set('last_time_asked', now_str)
            mcp_server.update_session(update_last_time)

        # Build the response string
        response = f"Current UTC time: {now_str}"
        if last_time_str:
            response += f"\nLast time asked: {last_time_str}"
            if seconds_since is not None:
                response += f"\nSeconds since last time: {seconds_since}"
        return response
    
    except Exception as e:
        # Catch-all for unexpected errors
        return f"Error: An unexpected error occurred: {str(e)}"

@mcp_server.tool()
def create_vpc(params: dict) -> dict:
    """
    Generalized VPC and Subnet Creator for OSS contribution.

    Parameters expected in `params`:
    - cidr_block: str
    - az_list: list[str]
    - public_subnets_per_az: int
    - private_subnets_per_az: int
    - name_tag: str

    e.g.
    createVpc(params={"cidr_block":"10.20.0.0/16","az_list":["us-west-2a","us-west-2b"],"public_subnets_per_az":1,"private_subnets_per_az":1,"name_tag":"mcp"})
    """
    ec2 = boto3.client("ec2", region_name="us-west-2")
    out = {}


    if isinstance(params, str):
        params = json.loads(params)
    # Extract parameters with defaults
    cidr_block = params.get("cidr_block", "10.0.0.0/16")
    az_list = params.get("az_list", ["us-west-2a", "us-west-2b"])
    public_subnets_per_az = params.get("public_subnets_per_az", 1)
    private_subnets_per_az = params.get("private_subnets_per_az", 1)
    name_tag = params.get("name_tag", "mcp-server")

    total_subnets_needed = (public_subnets_per_az + private_subnets_per_az) * len(az_list)
    subnet_blocks = list(ipaddress.ip_network(cidr_block).subnets(new_prefix=24))
    if len(subnet_blocks) < total_subnets_needed:
        raise Exception("Not enough subnet blocks in CIDR for requested subnets.")

    # Check existing VPC
    existing = ec2.describe_vpcs(Filters=[
        {"Name": "tag:Name", "Values": [f"{name_tag}-vpc"]}
    ])["Vpcs"]
    if existing:
        vpc_id = existing[0]["VpcId"]
        print(f"[INFO] Existing VPC found: {vpc_id}")
        out["VpcId"] = vpc_id
        return out

    # Create VPC
    vpc_id = ec2.create_vpc(CidrBlock=cidr_block)["Vpc"]["VpcId"]
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})
    ec2.create_tags(Resources=[vpc_id],
                    Tags=[{"Key": "Name", "Value": f"{name_tag}-vpc"}])
    out["VpcId"] = vpc_id

    # IGW and main route
    igw_id = ec2.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
    ec2.attach_internet_gateway(VpcId=vpc_id, InternetGatewayId=igw_id)
    out["InternetGatewayId"] = igw_id

    main_rt_id = ec2.describe_route_tables(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "association.main", "Values": ["true"]}
        ]
    )["RouteTables"][0]["RouteTableId"]
    ec2.create_route(RouteTableId=main_rt_id,
                     DestinationCidrBlock="0.0.0.0/0",
                     GatewayId=igw_id)

    out["PublicSubnets"] = []
    out["PrivateSubnets"] = []

    idx = 0
    for az in az_list:
        # Create public subnets
        for i in range(public_subnets_per_az):
            cidr_pub = str(subnet_blocks[idx])
            idx += 1
            pub_subnet = ec2.create_subnet(
                VpcId=vpc_id, CidrBlock=cidr_pub, AvailabilityZone=az,
                TagSpecifications=[{
                    "ResourceType": "subnet",
                    "Tags": [{"Key": "Name", "Value": f"{name_tag}-pub-{az}-{i}"}]
                }]
            )["Subnet"]
            pub_id = pub_subnet["SubnetId"]
            ec2.modify_subnet_attribute(SubnetId=pub_id, MapPublicIpOnLaunch={"Value": True})
            out["PublicSubnets"].append(pub_id)

        # Create NAT Gateway for private subnets
        eip = ec2.allocate_address(Domain="vpc")["AllocationId"]
        nat = ec2.create_nat_gateway(
            SubnetId=pub_id, AllocationId=eip,
            TagSpecifications=[{
                "ResourceType": "natgateway",
                "Tags": [{"Key": "Name", "Value": f"{name_tag}-nat-{az}"}]
            }]
        )["NatGateway"]
        nat_id = nat["NatGatewayId"]

        waiter = ec2.get_waiter("nat_gateway_available")
        print(f"Waiting for NAT Gateway {nat_id} in {az}...")
        waiter.wait(NatGatewayIds=[nat_id])
        print(f"NAT Gateway {nat_id} is available.")

        # Create private subnets
        for i in range(private_subnets_per_az):
            cidr_pri = str(subnet_blocks[idx])
            idx += 1
            pri_subnet = ec2.create_subnet(
                VpcId=vpc_id, CidrBlock=cidr_pri, AvailabilityZone=az,
                TagSpecifications=[{
                    "ResourceType": "subnet",
                    "Tags": [{"Key": "Name", "Value": f"{name_tag}-pri-{az}-{i}"}]
                }]
            )["Subnet"]
            pri_id = pri_subnet["SubnetId"]
            out["PrivateSubnets"].append(pri_id)

            rt_priv = ec2.create_route_table(VpcId=vpc_id)["RouteTable"]["RouteTableId"]
            ec2.associate_route_table(RouteTableId=rt_priv, SubnetId=pri_id)
            ec2.create_route(RouteTableId=rt_priv,
                             DestinationCidrBlock="0.0.0.0/0",
                             NatGatewayId=nat_id)

    return out

def ensure_eks_access_entries(eks, cluster_name, node_role_arn):
    sts = boto3.client("sts")
    access_entries = eks.list_access_entries(clusterName=cluster_name)["accessEntries"]
    existing_principals = [entry["principalArn"] for entry in access_entries]
    print("[INFO] Current Access Entries:", existing_principals)

    # Register Node Role
    if node_role_arn not in existing_principals:
        eks.create_access_entry(
            clusterName=cluster_name,
            principalArn=node_role_arn,
            type="EC2_LINUX"
        )
        print(f"[INFO] Access Entry registered for Node Role: {node_role_arn}")

@mcp_server.tool()
def create_eks_cluster_with_nodegroup(params: dict) -> dict:
    """
    Generalized EKS cluster + node group creator (OSS contribution ready).

    Accepts:
        params: dict containing optional keys:
            - cluster_name
            - nodegroup_name
            - version
            - instance_type
            - desired_size
            - min_size
            - max_size
            - public_cidrs
            - endpoint_public
            - endpoint_private
            - vpc_id
            - subnet_ids
            - security_group_ids
            - region
            - control_role_name
            - node_role_name
            - install_addons
    e.g.
    create_eks_cluster_with_nodegroup({
        "cluster_name": "mcp-eks-cluster",
        "nodegroup_name": "mcp-ng",
        "desired_size": 2,
        "min_size": 1,
        "max_size": 2,
        "region": "us-west-2"
    })
    """
    eks = boto3.client("eks", region_name=params.get("region", "us-west-2"))
    ec2 = boto3.client("ec2", region_name=params.get("region", "us-west-2"))
    iam = boto3.client("iam", region_name=params.get("region", "us-west-2"))

    cluster_name = params.get("cluster_name", "my-cluster")
    nodegroup_name = params.get("nodegroup_name", "default-ng")
    version = params.get("version", "1.32")
    instance_type = params.get("instance_type", "t3.medium")
    desired_size = params.get("desired_size", 2)
    min_size = params.get("min_size", 1)
    max_size = params.get("max_size", 3)
    public_cidrs = params.get("public_cidrs", ["0.0.0.0/0"])
    endpoint_public = params.get("endpoint_public", True)
    endpoint_private = params.get("endpoint_private", True)
    vpc_id = params.get("vpc_id")
    subnet_ids = params.get("subnet_ids")
    security_group_ids = params.get("security_group_ids")
    control_role_name = params.get("control_role_name", "EKSServiceRole")
    node_role_name = params.get("node_role_name", "EKSNodeRole")
    install_addons = params.get("install_addons", True)

    # 1️⃣ Check if cluster already exists
    if cluster_name in eks.list_clusters()["clusters"]:
        print(f"[INFO] EKS cluster '{cluster_name}' already exists. Skipping creation.")
        return {"ClusterName": cluster_name, "Status": "EXISTS"}

    # 2️⃣ IAM Roles
    try:
        control_role_arn = iam.get_role(RoleName=control_role_name)["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        print(f"[INFO] Creating IAM role: {control_role_name}")
        control_role = iam.create_role(
            RoleName=control_role_name,
            AssumeRolePolicyDocument=json.dumps({
                "Version": "2012-10-17",
                "Statement": [{
                    "Effect": "Allow",
                    "Principal": {"Service": "eks.amazonaws.com"},
                    "Action": "sts:AssumeRole"
                }]
            }),
            Description="IAM role for EKS control plane"
        )
        control_role_arn = control_role["Role"]["Arn"]
        iam.attach_role_policy(RoleName=control_role_name,
                               PolicyArn="arn:aws:iam::aws:policy/AmazonEKSClusterPolicy")

    try:
        node_role_arn = iam.get_role(RoleName=node_role_name)["Role"]["Arn"]
    except iam.exceptions.NoSuchEntityException:
        print(f"[INFO] Creating IAM role: {node_role_name}")
        node_role = iam.create_role(
            RoleName=node_role_name,
            AssumeRolePolicyDocument=json.dumps({
                "Version": "2012-10-17",
                "Statement": [{
                    "Effect": "Allow",
                    "Principal": {"Service": "ec2.amazonaws.com"},
                    "Action": "sts:AssumeRole"
                }]
            }),
            Description="IAM role for EKS worker nodes"
        )
        node_role_arn = node_role["Role"]["Arn"]
        for policy in [
            "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
            "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
            "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
        ]:
            iam.attach_role_policy(RoleName=node_role_name, PolicyArn=policy)

    # 3️⃣ VPC, Subnets, SG
    if not vpc_id:
        vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
        if not vpcs:
            raise Exception("No default VPC found and no vpc_id provided.")
        vpc_id = vpcs[0]["VpcId"]

    if not subnet_ids:
        subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}])["Subnets"]
        subnet_ids = [s["SubnetId"] for s in subnets]
        if len(subnet_ids) < 2:
            raise Exception("At least two subnets are required for EKS cluster creation.")

    if not security_group_ids:
        sgs = ec2.describe_security_groups(Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "group-name", "Values": ["default"]}
        ])["SecurityGroups"]
        if not sgs:
            raise Exception("No security group found in the VPC.")
        security_group_ids = [sgs[0]["GroupId"]]

    # 4️⃣ Create EKS Cluster
    print(f"[INFO] Creating EKS cluster '{cluster_name}'...")
    eks.create_cluster(
        name=cluster_name,
        version=version,
        roleArn=control_role_arn,
        resourcesVpcConfig={
            "subnetIds": subnet_ids,
            "securityGroupIds": security_group_ids,
            "endpointPublicAccess": endpoint_public,
            "endpointPrivateAccess": endpoint_private,
            "publicAccessCidrs": public_cidrs
        }
    )
    waiter = eks.get_waiter("cluster_active")
    print(f"[INFO] Waiting for EKS cluster '{cluster_name}' to become ACTIVE...")
    waiter.wait(name=cluster_name)
    print(f"[SUCCESS] EKS cluster '{cluster_name}' is ACTIVE.")

    # 5️⃣ Add-ons
    if install_addons:
        addon_versions = {
            "vpc-cni": "v1.19.2-eksbuild.1",
            "coredns": "v1.11.4-eksbuild.2",
            "kube-proxy": "v1.32.0-eksbuild.2",
            "eks-pod-identity-agent": "v1.3.4-eksbuild.1"
        }
        for addon, version_str in addon_versions.items():
            try:
                print(f"[INFO] Installing addon: {addon} ({version_str})...")
                eks.create_addon(
                    clusterName=cluster_name,
                    addonName=addon,
                    addonVersion=version_str,
                    resolveConflicts="OVERWRITE"
                )
            except eks.exceptions.ResourceInUseException:
                print(f"[WARN] Addon '{addon}' already exists. Skipping.")
            except Exception as e:
                print(f"[ERROR] Failed to install addon '{addon}': {e}")

    # 6️⃣ Register Access Entry
    print("[INFO] Registering EKS Access Entries...")
    ensure_eks_access_entries(eks, cluster_name, node_role_arn)
    print("[INFO] Access Entry registration completed.")

    waiter = eks.get_waiter('cluster_active')
    waiter.wait(name=cluster_name)

    # 7️⃣ Node Group
    print(f"[INFO] Creating node group '{nodegroup_name}' in '{cluster_name}'...")
    eks.create_nodegroup(
        clusterName=cluster_name,
        nodegroupName=nodegroup_name,
        scalingConfig={
            "minSize": min_size,
            "maxSize": max_size,
            "desiredSize": desired_size
        },
        subnets=subnet_ids,
        instanceTypes=[instance_type],
        nodeRole=node_role_arn,
        amiType="AL2023_x86_64_STANDARD",
        diskSize=20,
        capacityType="ON_DEMAND"
    )
    ng_waiter = eks.get_waiter("nodegroup_active")
    print(f"[INFO] Waiting for node group '{nodegroup_name}' to become ACTIVE...")
    ng_waiter.wait(clusterName=cluster_name, nodegroupName=nodegroup_name)
    print(f"[SUCCESS] Node group '{nodegroup_name}' is ACTIVE.")

    return {
        "ClusterName": cluster_name,
        "NodegroupName": nodegroup_name,
        "VPC": vpc_id,
        "Subnets": subnet_ids,
        "SecurityGroups": security_group_ids
    }

def lambda_handler(event, context):
    """AWS Lambda handler function."""
    return mcp_server.handle_request(event, context) 